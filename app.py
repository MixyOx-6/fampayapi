#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
============================================================================
Standalone FamPay UPI Payment & Gmail IMAP Verification API (Python 3.8+)
============================================================================
Buyer ID     : rby_3a9463d0f7736b27
Merchant     : Tell Chatai
Issued By    : Sharmaji
Generated At : 2026-10-28T20:03:13Z

Zero External Dependencies Required (uses Python standard library `imaplib`,
`email`, `http.server`, `json`, `hmac`, `hashlib`, `urllib`).

Run Server:
    python3 fampay_api.py

Endpoints:
    1. Create Order : POST/GET http://localhost:8080/v1/create?amount=100
    2. Check Status : GET      http://localhost:8080/v1/check?order_id=ord_...
    3. Verify Now   : POST     http://localhost:8080/v1/verify
    4. Health Check : GET      http://localhost:8080/health
============================================================================
"""

import os
import re
import json
import time
import hmac
import hashlib
import random
import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import urlencode, quote, urlparse, parse_qs
from urllib.request import Request, urlopen
from http.server import HTTPServer, BaseHTTPRequestHandler

# ============================================================================
# 1. STANDALONE BUYER FAMPAY CONFIGURATION
# ============================================================================
_DEFAULT_DIR = os.path.dirname(os.path.abspath(__file__))
_STORAGE_DIR = _DEFAULT_DIR if os.access(_DEFAULT_DIR, os.W_OK) else "/tmp"

CONFIG = {
    "fampay_upi": os.environ.get("FAMPAY_UPI_ID", "7908817900@fam"),
    "gmail_email": os.environ.get("GMAIL_EMAIL", "saifulmolla79088179@gmail.com"),
    "gmail_app_password": re.sub(r"\s+", "", os.environ.get("GMAIL_APP_PASSWORD", "lotvgkbsjazhthdy")),
    "merchant_name": os.environ.get("MERCHANT_NAME", "Tell Chatai"),
    "api_brand": os.environ.get("API_BRAND", "Sharmaji"),
    "api_key": os.environ.get("FAMPAY_API_KEY", "fp_live_5dd46fe835de54e91b6c4f9aabc0ea3d"),
    "webhook_url": os.environ.get("WEBHOOK_URL", ""),
    "webhook_secret": os.environ.get("WEBHOOK_SECRET", "whsec_2db71a9ee8f38b1d94aa568fbbfd461c"),
    "expiry_minutes": int(os.environ.get("EXPIRY_MINUTES", "15")),
    "port": int(os.environ.get("PORT", "8080")),
    "storage_file": os.path.join(_STORAGE_DIR, ".fampay_orders.json"),
}

FAMPAY_SENDER_HINTS = [
    "fampay", "famapp", "fam pay", "fam app", "yesfam", "yes bank",
    "idfc", "upi", "received", "credited", "payment", "credit alert",
    "ord_", "money received", "paid to you",
]


# ============================================================================
# 2. ORDER STORAGE & UPI QR BUILDER
# ============================================================================
def load_orders():
    if not os.path.isfile(CONFIG["storage_file"]):
        return {}
    try:
        with open(CONFIG["storage_file"], "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_orders(orders):
    try:
        with open(CONFIG["storage_file"], "w", encoding="utf-8") as f:
            json.dump(orders, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def reconstruct_serverless_order(order_id):
    m = re.match(r"^ord_(\d{10})_(\d+)_(\d+)$", str(order_id or "").strip())
    if not m:
        return None
    created_ts = int(m.group(1))
    paise = int(m.group(2))
    reference = m.group(3)
    amount = round(paise / 100.0, 2)
    exp_ts = created_ts + (CONFIG["expiry_minutes"] * 60)
    return {
        "id": order_id,
        "amount": amount,
        "payable_amount": amount,
        "provider": "fampay",
        "upi_id": CONFIG["fampay_upi"],
        "reference": reference,
        "utr": None,
        "status": "pending",
        "created_ts": created_ts,
        "expires_ts": exp_ts,
        "created_at": datetime.fromtimestamp(created_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": datetime.fromtimestamp(exp_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "paid_at": None,
    }


def build_upi_link(upi_id, amount, merchant_name, order_id, reference):
    note = order_id or reference or "Payment"
    tr = reference or order_id or ""
    return (
        f"upi://pay?pa={quote(upi_id)}"
        f"&pn={quote(merchant_name or 'Merchant')}"
        f"&am={amount:.2f}"
        f"&cu=INR"
        f"&tn={quote(note)}"
        f"&tr={quote(tr)}"
    )


# ============================================================================
# 3. FAMPAY EMAIL PARSING & MATCHING ENGINE (Exact 1:1 PayZapy Port)
# ============================================================================
def looks_like_fampay(sender, subject, body_preview):
    haystack = f"{sender} {subject} {body_preview[:800]}".lower()
    return any(hint in haystack for hint in FAMPAY_SENDER_HINTS)


def extract_fampay_txn_id(text):
    if not text:
        return None
    m = re.search(r"\b(FMP[A-Z0-9]{8,24})\b", text, re.IGNORECASE)
    return m.group(1) if m else None


def extract_utr(text):
    if not text:
        return None
    explicit = re.search(
        r"\b(?:UTR|RRN|UPI\s*Ref(?:erence)?(?:\s*No)?|Ref(?:erence)?\s*(?:No|Id|Number|#)?|Txn\s*(?:Id|Ref|Number)?)\s*[:\s#]*([0-9]{12})\b",
        text,
        re.IGNORECASE,
    )
    if explicit:
        return explicit.group(1)
    fmp = extract_fampay_txn_id(text)
    if fmp:
        return fmp
    bare = re.search(r"(?:^|[^A-Za-z0-9])([0-9]{12})(?:[^A-Za-z0-9]|$)", text)
    return bare.group(1) if bare else None


def extract_purpose(text):
    if not text:
        return None
    direct = re.search(r"\b((?:test_)?ord_?[a-zA-Z0-9]{6,32})\b", text, re.IGNORECASE)
    if direct:
        return direct.group(1)
    m = re.search(r"(?:Purpose|Note|Remark|Remarks|Description|Message)[:\s]+([A-Za-z0-9_\-]{6,40})", text, re.IGNORECASE)
    return m.group(1) if m else None


def extract_amount(text):
    if not text:
        return None
    patterns = [
        r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)",
        r"received\s*(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)",
        r"credited\s*(?:with|by|for)?\s*(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)",
        r"paid\s*(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)",
        r"amount\s*[:\-]?\s*(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m and m.group(1):
            try:
                val = float(m.group(1).replace(",", ""))
                if val > 0:
                    return val
            except ValueError:
                pass
    return None


def decode_mime_header_str(value):
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for content, enc in parts:
        if isinstance(content, bytes):
            decoded.append(content.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(str(content))
    return "".join(decoded)


def extract_message_body(msg):
    collected = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    collected.append(payload.decode(charset, errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            collected.append(payload.decode(charset, errors="replace"))

    raw_text = "\n".join(collected)
    raw_text = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", raw_text, flags=re.IGNORECASE)
    raw_text = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", raw_text, flags=re.IGNORECASE)
    raw_text = re.sub(r"<[^>]+>", " ", raw_text)
    raw_text = raw_text.replace("&#8377;", "₹").replace("&₹;", "₹")
    return re.sub(r"\s+", " ", raw_text).strip()


def normalize_ref(val):
    return re.sub(r"[^a-z0-9]", "", str(val or "").lower())


def amount_in_paise(val):
    try:
        return int(round(float(val) * 100))
    except Exception:
        return None


def is_eligible_mail(mail, order):
    email_paise = amount_in_paise(mail.get("amount"))
    order_paise = amount_in_paise(order.get("payable_amount") or order.get("amount"))
    if email_paise is None or order_paise is None or email_paise != order_paise:
        return False

    rec_ts = int(mail.get("received_ts") or 0)
    cre_ts = int(order.get("created_ts") or 0)
    exp_ts = int(order.get("expires_ts") or (cre_ts + 900))
    if not rec_ts or not cre_ts:
        return False

    return (cre_ts - 300) <= rec_ts <= (exp_ts + 300)


def matches_explicit_reference(mail, order):
    if not is_eligible_mail(mail, order):
        return False
    order_id = normalize_ref(order.get("id"))
    pay_ref = normalize_ref(order.get("reference") or order.get("utr"))
    m_oid = normalize_ref(mail.get("orderId"))
    m_raw = normalize_ref(mail.get("raw"))
    m_sub = normalize_ref(mail.get("subject"))

    if order_id and (m_oid == order_id or order_id in m_raw or order_id in m_sub):
        return True
    if pay_ref and len(pay_ref) >= 6 and (pay_ref in m_raw or pay_ref in m_sub):
        return True
    return False


def fetch_fampay_emails():
    user = CONFIG["gmail_email"].strip().lower()
    pw = re.sub(r"\s+", "", CONFIG["gmail_app_password"])
    debug = {"connected": False, "totalScanned": 0, "famMatched": 0, "error": None}
    results = []

    if not user or not pw:
        debug["error"] = "Missing Gmail address or 16-char App Password"
        return {"emails": results, "debug": debug}

    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=6)
        imap.login(user, pw)
        debug["connected"] = True
        imap.select("INBOX", readonly=True)

        typ, data = imap.search(None, "ALL")
        msg_ids = data[0].split() if (typ == "OK" and data and data[0]) else []
        newest_ids = list(reversed(msg_ids[-15:]))
        debug["totalScanned"] = len(newest_ids)

        for mid in newest_ids:
            typ, msg_data = imap.fetch(mid, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw_bytes = msg_data[0][1]
            msg = email.message_from_bytes(raw_bytes)
            subj = decode_mime_header_str(msg.get("Subject", ""))
            sender = decode_mime_header_str(msg.get("From", ""))
            date_hdr = msg.get("Date", "")
            try:
                dt = parsedate_to_datetime(date_hdr)
                rec_ts = int(dt.timestamp())
            except Exception:
                rec_ts = int(time.time())

            body_text = extract_message_body(msg)
            if looks_like_fampay(sender, subj, body_text):
                debug["famMatched"] += 1
                full_text = f"{subj}\n{body_text}"
                amt = extract_amount(full_text)
                utr = extract_utr(full_text)
                fmp = extract_fampay_txn_id(full_text)
                oid = extract_purpose(full_text)
                if amt is not None or utr is not None or fmp is not None or oid is not None:
                    results.append({
                        "utr": utr or fmp,
                        "fampayTxnId": fmp,
                        "orderId": oid,
                        "amount": amt,
                        "subject": subj,
                        "from": sender.lower(),
                        "received_ts": rec_ts,
                        "receivedAt": datetime.fromtimestamp(rec_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "raw": body_text[:600],
                    })
            if len(results) >= 5:
                break

        imap.logout()
    except Exception as exc:
        debug["error"] = str(exc)

    return {"emails": results, "debug": debug}


def verify_order_against_mailbox(order, all_orders):
    scan = fetch_fampay_emails()
    emails = scan["emails"]
    debug = scan["debug"]

    target_paise = amount_in_paise(order.get("payable_amount") or order.get("amount"))
    pending_same = [
        o for o in all_orders.values()
        if o.get("status") == "pending" and amount_in_paise(o.get("payable_amount") or o.get("amount")) == target_paise
    ]

    for mail in emails:
        matched = matches_explicit_reference(mail, order)
        if not matched and len(pending_same) == 1 and pending_same[0].get("id") == order.get("id"):
            matched = is_eligible_mail(mail, order)

        if matched:
            order["status"] = "paid"
            order["utr"] = mail.get("utr") or mail.get("fampayTxnId") or order.get("reference")
            order["paid_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            all_orders[order["id"]] = order
            save_orders(all_orders)

            if CONFIG["webhook_url"]:
                try:
                    payload = json.dumps({
                        "event": "payment.Success",
                        "status": "paid",
                        "order_id": order["id"],
                        "amount": order["amount"],
                        "utr": order["utr"],
                        "paid_at": order["paid_at"],
                    }).encode("utf-8")
                    sig = hmac.new(CONFIG["webhook_secret"].encode("utf-8"), payload, hashlib.sha256).hexdigest()
                    req = Request(
                        CONFIG["webhook_url"],
                        data=payload,
                        headers={"Content-Type": "application/json", "X-PayZapy-Signature": sig},
                        method="POST",
                    )
                    urlopen(req, timeout=4)
                except Exception:
                    pass

            return {"paid": True, "debug": debug}

    return {"paid": False, "debug": debug}


# ============================================================================
# 4. HTTP SERVER & VERCEL SERVERLESS HANDLER (`handler` class)
# ============================================================================
class handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key, Authorization")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key, Authorization")
        self.end_headers()

    def do_GET(self):
        self._handle_request("GET")

    def do_POST(self):
        self._handle_request("POST")

    def _handle_request(self, method):
        parsed = urlparse(self.path)
        query = {k.strip().lower(): v[0].strip() for k, v in parse_qs(parsed.query).items()}
        if "=" in parsed.path:
            tail = parsed.path.split("/")[-1]
            for k, v in parse_qs(tail).items():
                ck = k.strip().lower()
                if ck and ck not in query:
                    query[ck] = v[0].strip()
        raw_len = int(self.headers.get("Content-Length", "0") or "0")
        body = {}
        if raw_len > 0:
            try:
                body = json.loads(self.rfile.read(raw_len).decode("utf-8"))
            except Exception:
                body = {}

        action = (query.get("action") or body.get("action") or "").lower()
        if not action:
            if "/check" in parsed.path:
                action = "check"
            elif "/verify" in parsed.path:
                action = "verify"
            elif "/health" in parsed.path:
                action = "health"
            elif query.get("order_id") or body.get("order_id"):
                action = "check"
            else:
                action = "create"

        if action == "health":
            return self._send_json({
                "status": "ok",
                "provider": "fampay",
                "mode": "standalone_python",
                "fampay_upi": CONFIG["fampay_upi"],
                "gmail": CONFIG["gmail_email"],
                "api_by": CONFIG["api_brand"],
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })

        orders = load_orders()

        if action in ("check", "verify"):
            order_id = (query.get("order_id") or body.get("order_id") or query.get("id") or "").strip()
            if not order_id:
                return self._send_json({"status": "error", "message": "order_id parameter is required"}, 400)

            order = orders.get(order_id) or reconstruct_serverless_order(order_id)
            if not order:
                return self._send_json({"status": "error", "message": "Order not found"}, 404)

            orders[order_id] = order
            if order.get("status") == "pending" and int(time.time()) > int(order.get("expires_ts", 0)):
                order["status"] = "expired"
                orders[order_id] = order
                save_orders(orders)

            verify_debug = None
            if order.get("status") == "pending":
                res = verify_order_against_mailbox(order, orders)
                verify_debug = res.get("debug")

            return self._send_json({
                "status": order["status"],
                "order_id": order["id"],
                "amount": float(order["amount"]),
                "payable_amount": float(order["payable_amount"]),
                "provider": "fampay",
                "utr": order.get("utr"),
                "time": order.get("created_at"),
                "paid_at": order.get("paid_at"),
                "debug": verify_debug,
                "api_by": CONFIG["api_brand"],
            })

        # Create Order
        req_key = (
            self.headers.get("X-API-Key")
            or query.get("api_key")
            or query.get("apikey")
            or query.get("key")
            or body.get("api_key")
            or body.get("apikey")
            or body.get("key")
            or ""
        ).strip()
        auth_hdr = self.headers.get("Authorization", "")
        if not req_key and auth_hdr.lower().startswith("bearer "):
            req_key = auth_hdr[7:].strip()

        if CONFIG["api_key"] and req_key != CONFIG["api_key"]:
            return self._send_json({"status": "error", "code": "INVALID_API_KEY", "message": "Valid X-API-Key or api_key/apikey parameter is required."}, 401)

        try:
            amount = round(float(body.get("amount") or query.get("amount") or 0), 2)
        except Exception:
            amount = 0.0

        if amount < 1.0:
            return self._send_json({"status": "error", "message": "Invalid amount. Minimum amount is ₹1.00"}, 400)

        now_ts = int(time.time())
        exp_ts = now_ts + (CONFIG["expiry_minutes"] * 60)
        paise_val = int(round(amount * 100))
        reference = str(random.randint(100000000000, 999999999999))
        order_id = f"ord_{now_ts}_{paise_val}_{reference}"
        upi_link = build_upi_link(CONFIG["fampay_upi"], amount, CONFIG["merchant_name"], order_id, reference)
        qr_url = f"https://quickchart.io/qr?text={quote(upi_link)}&size=300"

        proto = self.headers.get("X-Forwarded-Proto", "http")
        host = self.headers.get("Host", f"localhost:{CONFIG['port']}")
        base_path = parsed.path if parsed.path and parsed.path != "/" else ""
        if base_path.endswith("/create"):
            check_path = base_path[:-7] + "/check"
            check_url = f"{proto}://{host}{check_path}?order_id={quote(order_id)}"
        elif base_path:
            check_url = f"{proto}://{host}{base_path}?action=check&order_id={quote(order_id)}"
        else:
            check_url = f"{proto}://{host}/?action=check&order_id={quote(order_id)}"

        new_order = {
            "id": order_id,
            "amount": amount,
            "payable_amount": amount,
            "provider": "fampay",
            "upi_id": CONFIG["fampay_upi"],
            "upi_link": upi_link,
            "qr_url": qr_url,
            "reference": reference,
            "utr": None,
            "status": "pending",
            "created_ts": now_ts,
            "expires_ts": exp_ts,
            "created_at": datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": datetime.fromtimestamp(exp_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "paid_at": None,
        }

        orders[order_id] = new_order
        save_orders(orders)

        return self._send_json({
            "status": "success",
            "order_id": order_id,
            "amount": amount,
            "payable_amount": amount,
            "provider": "fampay",
            "upi_id": CONFIG["fampay_upi"],
            "upi_link": upi_link,
            "qr_url": qr_url,
            "check_status_url": check_url,
            "expires_at": new_order["expires_at"],
            "api_by": CONFIG["api_brand"],
        }, 201)


FamPayAPIHandler = handler

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", CONFIG["port"]), handler)
    print(f"[FamPay Standalone Python API] Listening on http://0.0.0.0:{CONFIG['port']} (UPI: {CONFIG['fampay_upi']})")
    server.serve_forever()
