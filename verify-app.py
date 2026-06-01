"""
Email Verifier v2 — optimized
- 5s timeout (was 10s)
- Gmail/Yahoo/Hotmail: DNS only, skip slow SMTP
- 5 threads parallel (was 1)
"""
import csv, io, re, time, uuid, os, threading
import dns.resolver, smtplib
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
CORS(app)

EMAIL_REGEX        = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
DISPOSABLE_DOMAINS = {"mailinator.com","10minutemail.com","guerrillamail.com","tempmail.com",
                      "yopmail.com","throwaway.email","fakeinbox.com","trashmail.com"}
ROLE_PREFIXES      = {"info","support","admin","sales","contact","noreply","no-reply",
                      "newsletter","marketing","hello","team","help","office","webmaster",
                      "postmaster","abuse","billing","donotreply","do-not-reply"}

# These providers block SMTP verify → skip, just check DNS/MX
SKIP_SMTP_DOMAINS  = {
    "gmail.com","googlemail.com","yahoo.com","yahoo.co.uk","yahoo.co.in",
    "hotmail.com","outlook.com","live.com","msn.com","icloud.com",
    "me.com","mac.com","aol.com","protonmail.com","proton.me"
}

data = {}
TIMEOUT = 5  # was 10

def smtp_check(email, mx):
    try:
        s = smtplib.SMTP(timeout=TIMEOUT)
        s.connect(mx)
        s.helo("mail.verify.io")
        s.mail("verify@verify.io")
        code, _ = s.rcpt(email)
        s.quit()
        return code
    except:
        return None

def check_email(email):
    if not EMAIL_REGEX.match(email):
        return "invalid", "bad_syntax"

    domain = email.split("@")[1].lower()
    local  = email.split("@")[0].lower()

    if domain in DISPOSABLE_DOMAINS:
        return "invalid", "disposable_domain"
    if local in ROLE_PREFIXES:
        return "invalid", "role_based"

    try:
        recs = dns.resolver.resolve(domain, "MX", lifetime=TIMEOUT)
        mx   = str(sorted(recs, key=lambda r: r.preference)[0].exchange).rstrip(".")
    except:
        return "invalid", "no_mx"

    # Skip SMTP for major providers — always risky, slow to check
    if domain in SKIP_SMTP_DOMAINS:
        return "risky", "major_provider_skip_smtp"

    # Catch-all check
    try:
        s = smtplib.SMTP(timeout=TIMEOUT)
        s.connect(mx)
        s.helo("mail.verify.io")
        s.mail("verify@verify.io")
        code, _ = s.rcpt(f"zzrndtest8812@{domain}")
        s.quit()
        if code == 250:
            return "risky", "domain_accepts_all"
    except:
        pass

    code = smtp_check(email, mx)
    if code in [421, 450, 451, 452, 503]:
        time.sleep(2)  # was 5
        code = smtp_check(email, mx)

    if code == 250: return "valid",   "smtp_ok"
    if code == 550: return "invalid", "smtp_reject"
    if code is None: return "risky",  "smtp_timeout"
    return "risky", f"smtp_{code}"

@app.route("/ping")
def ping():
    return jsonify({"ok": True})

@app.route("/verify", methods=["POST"])
def verify():
    job_id  = str(uuid.uuid4())
    file    = request.files["file"]
    content = file.read().decode("utf-8", errors="replace")
    reader  = list(csv.DictReader(io.StringIO(content)))
    if not reader:
        return jsonify({"error": "Empty CSV"}), 400

    email_field = next((f for f in reader[0].keys() if "email" in f.lower()), None)
    if not email_field:
        return jsonify({"error": "No email column found"}), 400

    total      = len(reader)
    output     = io.StringIO()
    fieldnames = list(reader[0].keys()) + ["status", "reason"]
    writer     = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    lock = threading.Lock()

    data[job_id] = {
        "progress": 0, "row": 0, "total": total,
        "log": "", "cancel": False,
        "output": output, "writer": writer,
        "records": reader, "email_field": email_field,
        "filename": file.filename, "done": False
    }

    def process_one(args):
        i, row = args
        if data[job_id]["cancel"]:
            return
        email = (row.get(email_field) or "").strip()
        if not email:
            status, reason = "invalid", "empty_email"
        else:
            status, reason = check_email(email)
        row["status"] = status
        row["reason"]  = reason
        with lock:
            writer.writerow(row)
            done_count = data[job_id]["row"] + 1
            pct = int(done_count / total * 100)
            data[job_id].update({
                "progress": pct, "row": done_count,
                "log": f"{email} -> {status} ({reason})"
            })

    def run():
        with ThreadPoolExecutor(max_workers=5) as ex:
            ex.map(process_one, enumerate(reader, 1))
        data[job_id]["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id, "total": total})

@app.route("/progress")
def progress():
    d = data.get(request.args.get("job_id"), {})
    return jsonify({
        "percent": d.get("progress", 0),
        "row":     d.get("row", 0),
        "total":   d.get("total", 0),
        "done":    d.get("done", False)
    })

@app.route("/log")
def log():
    return Response(data.get(request.args.get("job_id"), {}).get("log", ""), mimetype="text/plain")

@app.route("/cancel", methods=["POST"])
def cancel():
    jid = request.args.get("job_id")
    if jid in data: data[jid]["cancel"] = True
    return "", 204

@app.route("/download")
def download():
    jid   = request.args.get("job_id")
    ftype = request.args.get("type", "all")
    job   = data.get(jid)
    if not job: return "Job not found", 404
    job["output"].seek(0)
    reader = list(csv.DictReader(job["output"]))
    if not reader: return "No data", 404
    if ftype == "valid":            filtered = [r for r in reader if r["status"] == "valid"]
    elif ftype == "risky":          filtered = [r for r in reader if r["status"] == "risky"]
    elif ftype == "risky_invalid":  filtered = [r for r in reader if r["status"] in ("risky","invalid")]
    else:                           filtered = reader
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=reader[0].keys())
    w.writeheader(); w.writerows(filtered)
    base = job["filename"].replace(".csv","").replace(".CSV","")
    return Response(out.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{ftype}-{base}.csv"'})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
