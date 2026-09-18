"""
KPI Agent — BTP Production Backend
Flask + PostgreSQL (shared prompt-db) + SAP AI Core
"""
import os, json, uuid, re, base64, logging
from datetime import datetime
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import psycopg2
import psycopg2.extras
import requests

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "kpi-agent-btp-2026")

# ── VCAP_SERVICES parsing ─────────────────────────────────────────────────────

def get_vcap():
    raw = os.environ.get("VCAP_SERVICES", "{}")
    return json.loads(raw)

def get_db_creds():
    vcap = get_vcap()
    # shared postgresql-db binding (prompt-db shared into this space)
    for svc in vcap.get("postgresql-db", []):
        return svc["credentials"]
    # user-provided-service fallback
    for svc in vcap.get("user-provided", []):
        if "db" in svc.get("name", "").lower():
            return svc["credentials"]
    # local dev fallback
    return {
        "hostname": os.environ.get("DB_HOST", "localhost"),
        "port": os.environ.get("DB_PORT", "5432"),
        "dbname": os.environ.get("DB_NAME", "kpiagent"),
        "username": os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("DB_PASS", ""),
    }

def get_aicore_creds():
    vcap = get_vcap()
    for svc in vcap.get("aicore", []):
        return svc.get("credentials", {})
    return {
        "clientid": os.environ.get("AICORE_CLIENT_ID", ""),
        "clientsecret": os.environ.get("AICORE_CLIENT_SECRET", ""),
        "url": os.environ.get("AICORE_TOKEN_URL", ""),
        "serviceurls": {"AI_API_URL": os.environ.get("AICORE_API_URL", "")}
    }

# ── DB connection ─────────────────────────────────────────────────────────────

def get_conn():
    c = get_db_creds()
    return psycopg2.connect(
        host=c["hostname"],
        port=int(c["port"]),
        dbname=c["dbname"],
        user=c["username"],
        password=c["password"],
        sslmode="require",
        connect_timeout=10
    )

def init_db():
    """Create kpi_ tables if they don't exist."""
    ddl = """
    CREATE TABLE IF NOT EXISTS kpi_users (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        role        TEXT NOT NULL,
        region      TEXT,
        user_type   TEXT NOT NULL DEFAULT 'leader',
        managed_role TEXT,
        created_at  TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS kpi_definitions (
        id          TEXT NOT NULL,
        role        TEXT NOT NULL,
        label       TEXT NOT NULL,
        target      TEXT,
        category    TEXT,
        sort_order  INT DEFAULT 0,
        PRIMARY KEY (id, role)
    );

    CREATE TABLE IF NOT EXISTS kpi_history (
        hist_key    TEXT PRIMARY KEY,
        role        TEXT NOT NULL,
        region      TEXT NOT NULL,
        month       TEXT NOT NULL,
        kpi_data    JSONB NOT NULL,
        created_at  TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS kpi_submissions (
        sub_key     TEXT PRIMARY KEY,
        leader_id   TEXT NOT NULL,
        leader_name TEXT NOT NULL,
        role        TEXT NOT NULL,
        region      TEXT NOT NULL,
        month       TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'pending_approval',
        kpi_data    JSONB NOT NULL,
        submitted_at TIMESTAMP DEFAULT NOW(),
        approved_at  TIMESTAMP,
        approved_by  TEXT
    );

    CREATE TABLE IF NOT EXISTS kpi_regions (
        region      TEXT PRIMARY KEY,
        label       TEXT NOT NULL
    );
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
    log.info("DB schema initialised")
    _seed_data()

def _seed_data():
    """Seed reference data if tables are empty."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM kpi_users")
            count = cur.fetchone()[0]
            if count > 0:
                return  # already seeded

            # Regions
            regions = [
                ("MEE", "Middle East & Europe"),
                ("AMER", "Americas"),
                ("APAC", "Asia Pacific"),
                ("EMEA", "EMEA"),
            ]
            cur.executemany(
                "INSERT INTO kpi_regions (region, label) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                regions
            )

            # Users
            users = [
                ("dan_fagan",      "Dan Fagan",         "EA",  "MEE",  "leader", None),
                ("sara_mitchell",  "Sara Mitchell",     "EA",  "AMER", "leader", None),
                ("liu_wei",        "Liu Wei",           "EA",  "APAC", "leader", None),
                ("anna_bergmann",  "Anna Bergmann",     "EA",  "EMEA", "leader", None),
                ("raj_sharma",     "Raj Sharma",        "DA",  "MEE",  "leader", None),
                ("tom_bradley",    "Tom Bradley",       "DA",  "AMER", "leader", None),
                ("mei_tanaka",     "Mei Tanaka",        "CEP", "APAC", "leader", None),
                ("alex_sieferer",  "Alexander Sieferer","CEP", "EMEA", "leader", None),
                ("thomas_pfister", "Thomas Pfister",    "EA",  None,   "sponsor","EA"),
                ("jan_manos",      "Jan Manos",         "DA",  None,   "sponsor","DA"),
                ("bina_goleminova","Bina Goleminova",   "CEP", None,   "sponsor","CEP"),
            ]
            cur.executemany(
                "INSERT INTO kpi_users (id, name, role, region, user_type, managed_role) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                users
            )

            # KPI definitions
            ea_kpis = [
                ("ea_joule",     "EA", "Joule Activation Rate",      "90%",   "Adoption",     1),
                ("ea_ai_plan",   "EA", "AI Adoption Plan Submitted",  "100%",  "Planning",     2),
                ("ea_gainsight", "EA", "Gainsight Completion",        "85%",   "Engagement",   3),
                ("ea_coverage",  "EA", "EA Customer Coverage",        "95%",   "Coverage",     4),
                ("ea_enablement","EA", "Q3 Enablement Sessions",      "4/qtr", "Enablement",   5),
                ("ea_pipeline",  "EA", "Pipeline Growth vs Q2",       "+15%",  "Commercial",   6),
            ]
            da_kpis = [
                ("da_adoption",  "DA", "Digital Adoption Score",      "80+",   "Adoption",     1),
                ("da_nps",       "DA", "NPS Customer Survey",         "45+",   "Satisfaction", 2),
                ("da_coverage",  "DA", "DA Coverage by Segment",      "90%",   "Coverage",     3),
                ("da_enablement","DA", "Partner Enablement Complete", "100%",  "Enablement",   4),
                ("da_pipeline",  "DA", "New Pipeline Generated",      "$2M",   "Commercial",   5),
            ]
            cep_kpis = [
                ("cep_csat",     "CEP", "CSAT Score",                 "4.2/5", "Satisfaction", 1),
                ("cep_adoption", "CEP", "CEP Adoption Rate",          "75%",   "Adoption",     2),
                ("cep_coverage", "CEP", "Executive Sponsor Coverage", "80%",   "Coverage",     3),
                ("cep_pipeline", "CEP", "Upsell Pipeline",            "$1.5M", "Commercial",   4),
            ]
            all_kpis = ea_kpis + da_kpis + cep_kpis
            cur.executemany(
                "INSERT INTO kpi_definitions (id, role, label, target, category, sort_order) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                all_kpis
            )

            # History (Aug 2026)
            history_rows = [
                ("EA_MEE_2026_08", "EA", "MEE", "2026_08", json.dumps({
                    "ea_joule":     {"value": "82%",  "narrative": "Joule is activated for 82% of EA customers in MEE. Targeting 90% by end of Q3."},
                    "ea_ai_plan":   {"value": "75%",  "narrative": "AI adoption plans submitted for 75% of accounts. Remaining 25% in progress."},
                    "ea_gainsight": {"value": "78%",  "narrative": "Gainsight task completion at 78%. Improving with weekly follow-ups."},
                    "ea_coverage":  {"value": "88%",  "narrative": "EA coverage at 88% of target accounts. Two large accounts onboarding in September."},
                    "ea_enablement":{"value": "3/4",  "narrative": "3 enablement sessions completed this quarter. Final one scheduled for mid-September."},
                    "ea_pipeline":  {"value": "+9%",  "narrative": "Pipeline grew 9% vs Q2. On track but need to accelerate in key segments."},
                })),
                ("EA_AMER_2026_08", "EA", "AMER", "2026_08", json.dumps({
                    "ea_joule":     {"value": "91%",  "narrative": "AMER leads globally with 91% Joule activation. All major accounts live."},
                    "ea_ai_plan":   {"value": "100%", "narrative": "All 42 AMER accounts have AI adoption plans submitted and reviewed."},
                    "ea_gainsight": {"value": "92%",  "narrative": "Gainsight compliance is strong at 92%. Monitoring for consistency."},
                    "ea_coverage":  {"value": "98%",  "narrative": "Near full coverage at 98%. One account on hold due to M&A."},
                    "ea_enablement":{"value": "4/4",  "narrative": "All 4 Q3 enablement sessions delivered on schedule."},
                    "ea_pipeline":  {"value": "+18%", "narrative": "Pipeline up 18% vs Q2 — strong momentum from new deals in financial services."},
                })),
                ("DA_MEE_2026_08", "DA", "MEE", "2026_08", json.dumps({
                    "da_adoption":  {"value": "74",   "narrative": "Digital adoption score at 74. Need to reach 80 by quarter end."},
                    "da_nps":       {"value": "41",   "narrative": "NPS at 41, slightly below 45 target. Addressing feedback from three key accounts."},
                    "da_coverage":  {"value": "82%",  "narrative": "DA coverage at 82% across segments. White space identified in mid-market."},
                    "da_enablement":{"value": "90%",  "narrative": "Partner enablement 90% complete. Final certification pending for 2 partners."},
                    "da_pipeline":  {"value": "$1.6M","narrative": "$1.6M new pipeline generated in August, on track for $2M quarterly target."},
                })),
            ]
            cur.executemany(
                "INSERT INTO kpi_history (hist_key, role, region, month, kpi_data) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                history_rows
            )
        conn.commit()
    log.info("Reference data seeded")

# ── AI Core helper ────────────────────────────────────────────────────────────

_aicore_token = None
_token_expires = 0

def get_aicore_token():
    global _aicore_token, _token_expires
    now = datetime.now().timestamp()
    if _aicore_token and now < _token_expires - 60:
        return _aicore_token
    creds = get_aicore_creds()
    token_url = creds.get("url", "") + "/oauth/token"
    client_id = creds.get("clientid", "")
    client_secret = creds.get("clientsecret", "")
    if not client_id:
        return None
    try:
        resp = requests.post(
            token_url,
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        _aicore_token = data["access_token"]
        _token_expires = now + data.get("expires_in", 3600)
        return _aicore_token
    except Exception as e:
        log.warning("AI Core token fetch failed: %s", e)
        return None

def ai_core_chat(messages, model="gpt-4o"):
    """Call AI Core LLM via AI API."""
    creds = get_aicore_creds()
    api_url = creds.get("serviceurls", {}).get("AI_API_URL", "")
    if not api_url:
        return None
    token = get_aicore_token()
    if not token:
        return None
    try:
        deployment_url = f"{api_url}/v2/inference/deployments/auto/chat/completions"
        resp = requests.post(
            deployment_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "AI-Resource-Group": "default",
            },
            json={"model": model, "messages": messages, "max_tokens": 512},
            timeout=30
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning("AI Core call failed: %s", e)
        return None

# ── DB query helpers ──────────────────────────────────────────────────────────

def db_get_users():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM kpi_users ORDER BY user_type, role, name")
            return cur.fetchall()

def db_get_kpis(role):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM kpi_definitions WHERE role=%s ORDER BY sort_order", (role,))
            return cur.fetchall()

def db_get_regions():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT region FROM kpi_regions ORDER BY region")
            return [r["region"] for r in cur.fetchall()]

def db_get_history(role, region, month="2026_08"):
    key = f"{role}_{region}_{month}"
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT kpi_data FROM kpi_history WHERE hist_key=%s", (key,))
            row = cur.fetchone()
            return row["kpi_data"] if row else {}

def db_save_submission(leader, role, region, kpi_responses):
    sub_key = f"{role}_{region}_2026_09_{leader['id']}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO kpi_submissions (sub_key, leader_id, leader_name, role, region, month, kpi_data)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sub_key) DO UPDATE SET
                    kpi_data = EXCLUDED.kpi_data,
                    submitted_at = NOW(),
                    status = 'pending_approval'
            """, (sub_key, leader["id"], leader["name"], role, region, "September 2026",
                  json.dumps(kpi_responses)))
        conn.commit()

def db_get_submissions(role):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM kpi_submissions WHERE role=%s ORDER BY submitted_at DESC",
                (role,)
            )
            return cur.fetchall()

def db_approve_role(role, approved_by):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE kpi_submissions
                SET status='approved', approved_at=NOW(), approved_by=%s
                WHERE role=%s AND status='pending_approval'
            """, (approved_by, role))
        conn.commit()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    try:
        users = db_get_users()
    except Exception as e:
        log.error("DB error on login: %s", e)
        users = []
    leaders = [u for u in users if u["user_type"] == "leader"]
    sponsors = [u for u in users if u["user_type"] == "sponsor"]
    if request.method == "POST":
        uid = request.form.get("user_id")
        all_users = list(users)
        user = next((dict(u) for u in all_users if u["id"] == uid), None)
        if user:
            session["user"] = user
            if user["user_type"] == "sponsor":
                session["role_type"] = "sponsor"
                return redirect(url_for("sponsor_dashboard"))
            else:
                session["role_type"] = "leader"
                return redirect(url_for("agent_start"))
    return render_template("login.html",
        leaders=leaders, sponsors=sponsors,
        regional_leaders=leaders, global_sponsors=sponsors)

@app.route("/agent", methods=["GET"])
def agent_start():
    if "user" not in session:
        return redirect(url_for("login"))
    user = session["user"]
    try:
        kpis = db_get_kpis(user["role"])
        prior = db_get_history(user["role"], user["region"])
    except Exception as e:
        log.error("DB error on agent start: %s", e)
        kpis, prior = [], {}
    session["kpis"] = [dict(k) for k in kpis]
    session["kpi_index"] = 0
    session["responses"] = {}
    session["prior"] = dict(prior) if prior else {}
    role_labels = {"EA": "Enterprise Architect", "DA": "Digital Advisor", "CEP": "Customer Engagement Partner"}
    return render_template("agent.html",
        user=user,
        role_name=role_labels.get(user["role"], user["role"]),
        total_kpis=len(kpis),
        prior_month="August 2026"
    )

@app.route("/api/chat", methods=["POST"])
def chat():
    if "user" not in session:
        return jsonify({"error": "not logged in"}), 401

    data = request.json
    user_msg = (data.get("message") or "").strip()
    kpis = session.get("kpis", [])
    idx = session.get("kpi_index", 0)
    responses = session.get("responses", {})
    prior = session.get("prior", {})
    user = session["user"]
    role_labels = {"EA": "Enterprise Architect", "DA": "Digital Advisor", "CEP": "Customer Engagement Partner"}
    role_name = role_labels.get(user["role"], user["role"])

    if idx == 0 and not user_msg:
        return jsonify({
            "type": "greeting",
            "text": (f"Hi {user['name'].split()[0]}! It's time for your <strong>{role_name} "
                     f"status update</strong> for <strong>{user['region']}</strong> — September 2026.<br><br>"
                     f"I'll walk you through <strong>{len(kpis)} KPIs</strong> one by one. "
                     f"For each one I'll show what you said last month so you can update or confirm.<br><br>"
                     f"Ready to begin?"),
            "show_start": True
        })

    if user_msg.lower() in ["start", "yes", "begin", "ready", "let's go"]:
        session["kpi_index"] = 0
        return jsonify(_build_kpi_prompt(kpis, 0, prior))

    if idx < len(kpis):
        kpi = kpis[idx]
        kpi_id    = kpi["id"]    if isinstance(kpi, dict) else kpi[0]
        kpi_label = kpi["label"] if isinstance(kpi, dict) else kpi[2]
        kpi_tgt   = kpi["target"] if isinstance(kpi, dict) else kpi[3]

        prior_entry = prior.get(kpi_id, {})
        prior_narrative = prior_entry.get("narrative", "") if prior_entry else ""
        prior_value = prior_entry.get("value", "") if prior_entry else ""

        # Skip: don't call AI, just record as skipped
        if user_msg.upper() == "[SKIP]":
            resolved_narrative = "[Skipped — no update this month]"
            ai_ack = ""
        else:
            # AI interprets the user message in context and returns resolved narrative + ack
            resolved_narrative, ai_ack = _ai_interpret_kpi_response(
                kpi_label=kpi_label,
                kpi_target=kpi_tgt,
                user_msg=user_msg,
                prior_narrative=prior_narrative,
                prior_value=prior_value,
            )

        responses[kpi_id] = {
            "value": _extract_value(resolved_narrative),
            "narrative": resolved_narrative,
            "timestamp": datetime.now().isoformat()
        }
        session["responses"] = responses
        session["kpi_index"] = idx + 1
        next_idx = idx + 1

        if next_idx < len(kpis):
            reply = _build_kpi_prompt(kpis, next_idx, prior)
            if ai_ack:
                reply["acknowledgement"] = ai_ack
            # Show resolved text only if AI changed it from what the user typed
            if resolved_narrative.strip().lower() != user_msg.strip().lower():
                reply["resolved_narrative"] = resolved_narrative
            return jsonify(reply)
        else:
            kpi_labels = {
                (k["id"] if isinstance(k, dict) else k[0]): (k["label"] if isinstance(k, dict) else k[2])
                for k in kpis
            }
            summary_lines = "".join(
                f"<b>{kpi_labels.get(kid, kid)}:</b> {v['narrative']}<br>"
                for kid, v in responses.items()
            )
            summary_resp = {
                "type": "summary",
                "text": (f"All {len(kpis)} KPIs captured! Here's your summary:<br><br>" +
                         summary_lines +
                         "<br>Ready to submit to your Global Role Sponsor?"),
                "show_submit": True
            }
            if ai_ack:
                summary_resp["acknowledgement"] = ai_ack
            if resolved_narrative.strip().lower() != user_msg.strip().lower():
                summary_resp["resolved_narrative"] = resolved_narrative
            return jsonify(summary_resp)

    return jsonify({"type": "message", "text": "Please use the Submit button to finalise."})

def _ai_interpret_kpi_response(kpi_label, kpi_target, user_msg, prior_narrative, prior_value):
    """
    Use AI to resolve what the user actually meant and produce:
    - resolved_narrative: the final text to save (may reuse prior text if user said "same")
    - ack: one short encouraging sentence to show the user
    Falls back gracefully if AI Core is unavailable.
    """
    prior_context = ""
    if prior_narrative:
        prior_context = f'\nLast month update: "{prior_narrative}" (value: {prior_value})'

    system_prompt = (
        "You are a KPI data collection assistant. Your job is to interpret what a regional leader "
        "typed in response to a KPI question, and return a JSON object with two keys:\n"
        "  \"narrative\": the actual update text to record (a full, clear sentence — not just a number).\n"
        "  \"ack\": one short encouraging sentence (max 12 words) to show the user, confirming what was captured.\n\n"
        "Important rules:\n"
        "- If the user says 'same', 'same as last month', 'no change', 'use last month', 'copy', 'reuse', "
        "  or anything meaning they want to repeat last month's text — set narrative to the exact last month text.\n"
        "- If the user gives a short number or %, expand it into a proper sentence using context from the KPI name and target.\n"
        "- If the user gives a clear full sentence, use it as-is.\n"
        "- Always return valid JSON only. No markdown, no explanation outside the JSON.\n"
        "Example: {\"narrative\": \"Joule activation reached 88% this month, close to our 90% target.\", \"ack\": \"Got it — 88% noted, nearly at target!\"}"
    )

    user_prompt = (
        f"KPI: {kpi_label}\n"
        f"Target: {kpi_target}"
        f"{prior_context}\n"
        f"User said: \"{user_msg}\""
    )

    raw = ai_core_chat([
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ])

    if raw:
        try:
            # strip markdown fences if present
            cleaned = raw.strip().strip("```json").strip("```").strip()
            data = json.loads(cleaned)
            narrative = data.get("narrative", "").strip() or user_msg
            ack = data.get("ack", "").strip()
            return narrative, ack
        except Exception:
            pass

    # fallback: if user said "same"/"no change", reuse prior narrative
    same_phrases = ["same", "same as last month", "no change", "use last month",
                    "copy", "reuse", "keep same", "as before", "unchanged"]
    if user_msg.lower().strip() in same_phrases or any(p in user_msg.lower() for p in same_phrases):
        narrative = prior_narrative if prior_narrative else user_msg
        return narrative, "Reusing last month's update."

    return user_msg, ""


def _build_kpi_prompt(kpis, idx, prior):
    kpi = kpis[idx]
    kpi_id    = kpi["id"]    if isinstance(kpi, dict) else kpi[0]
    kpi_label = kpi["label"] if isinstance(kpi, dict) else kpi[2]
    kpi_tgt   = kpi["target"]   if isinstance(kpi, dict) else kpi[3]
    kpi_cat   = kpi["category"] if isinstance(kpi, dict) else kpi[4]
    total = len(kpis)

    prior_entry = prior.get(kpi_id, {})
    prior_html = ""
    if prior_entry:
        narrative = prior_entry.get("narrative", "")
        value = prior_entry.get("value", "")
        prior_html = (f'<div class="prior-box">'
                      f'<span class="prior-label">Last month ({value}):</span><br>'
                      f'<em>"{narrative}"</em></div>')

    return {
        "type": "kpi",
        "text": (f'<div class="kpi-header">'
                 f'<span class="kpi-num">KPI {idx+1} of {total}</span> — '
                 f'<span class="kpi-name">{kpi_label}</span> '
                 f'<span class="kpi-target">Target: {kpi_tgt}</span> '
                 f'<span class="kpi-cat">{kpi_cat}</span>'
                 f'</div>' + prior_html +
                 f"<br>What's your update for this month?"),
        "kpi_id": kpi_id,
        "kpi_index": idx,
        "total": total,
        "progress": round((idx / total) * 100)
    }

def _extract_value(text):
    m = re.search(r'(\d+\.?\d*\s*%|\$[\d,.]+[KMB]?|\d+\.\d+|\d+)', text)
    return m.group(1) if m else "—"

@app.route("/api/submit", methods=["POST"])
def submit():
    if "user" not in session:
        return jsonify({"error": "not logged in"}), 401
    user = session["user"]
    responses = session.get("responses", {})
    try:
        db_save_submission(user, user["role"], user["region"], responses)
    except Exception as e:
        log.error("Submit DB error: %s", e)
        return jsonify({"error": "DB error, please retry"}), 500
    session.clear()
    return jsonify({
        "success": True,
        "text": (f"Submitted! Your {user['role']} status for {user['region']} "
                 f"has been sent to the Global Role Sponsor for review.")
    })

@app.route("/sponsor")
def sponsor_dashboard():
    if "user" not in session or session.get("role_type") != "sponsor":
        return redirect(url_for("login"))
    user = session["user"]
    managed_role = user.get("managed_role") or user.get("role")
    try:
        regions = db_get_regions()
        kpis = db_get_kpis(managed_role)
        live_subs = db_get_submissions(managed_role)
    except Exception as e:
        log.error("Sponsor DB error: %s", e)
        regions, kpis, live_subs = ["MEE", "AMER", "APAC", "EMEA"], [], []

    live_by_region = {}
    for s in live_subs:
        region = s["region"] if isinstance(s, dict) else s[5]
        live_by_region[region] = dict(s) if isinstance(s, dict) else {
            "region": s[5], "status": s[6], "kpi_data": s[7]
        }

    all_subs = {}
    for region in regions:
        if region in live_by_region:
            row = live_by_region[region]
            # normalize kpi_data → kpis for template compatibility
            kpi_payload = row.get("kpi_data", {})
            if isinstance(kpi_payload, str):
                try:
                    kpi_payload = json.loads(kpi_payload)
                except Exception:
                    kpi_payload = {}
            row["kpis"] = kpi_payload
            all_subs[region] = {"source": "live", "data": row}
        else:
            hist = db_get_history(managed_role, region)
            if hist:
                if isinstance(hist, str):
                    try:
                        hist = json.loads(hist)
                    except Exception:
                        hist = {}
                all_subs[region] = {"source": "sample", "data": {"kpis": hist, "status": "sample"}}
            else:
                all_subs[region] = {"source": "pending", "data": None}

    role_labels = {"EA": "Enterprise Architect", "DA": "Digital Advisor", "CEP": "Customer Engagement Partner"}
    return render_template("sponsor.html",
        user=user,
        managed_role=managed_role,
        role_name=role_labels.get(managed_role, managed_role),
        kpis=[dict(k) for k in kpis],
        regions=regions,
        all_subs=all_subs
    )

@app.route("/api/approve", methods=["POST"])
def approve():
    if "user" not in session:
        return jsonify({"error": "not logged in"}), 401
    data = request.json
    role = data.get("role")
    try:
        db_approve_role(role, session["user"]["name"])
    except Exception as e:
        log.error("Approve error: %s", e)
        return jsonify({"error": "DB error"}), 500
    return jsonify({"success": True, "message": f"All {role} submissions approved."})

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── Admin ─────────────────────────────────────────────────────────────────────

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "sap2026")

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_panel"))
        return render_template("admin_login.html", error="Incorrect password.")
    return render_template("admin_login.html", error=None)

@app.route("/admin")
def admin_panel():
    if not session.get("is_admin"):
        return redirect(url_for("admin_login"))
    return render_template("admin.html")

@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("login"))

def _require_admin():
    if not session.get("is_admin"):
        from flask import abort
        abort(401)

# ── Admin API: Users ──────────────────────────────────────────────────────────

@app.route("/admin/api/users", methods=["GET"])
def admin_get_users():
    _require_admin()
    users = db_get_users()
    leaders  = [dict(u) for u in users if u["user_type"] == "leader"]
    sponsors = [dict(u) for u in users if u["user_type"] == "sponsor"]
    return jsonify({"leaders": leaders, "sponsors": sponsors})

@app.route("/admin/api/users", methods=["POST"])
def admin_add_user():
    _require_admin()
    data = request.json
    name        = data.get("name", "").strip()
    role        = data.get("role", "").strip()
    region      = data.get("region", "").strip() or None
    user_type   = data.get("user_type", "leader")
    managed_role = data.get("managed_role", role if user_type == "sponsor" else None)
    email       = data.get("email", "").strip()
    if not name or not role:
        return jsonify({"error": "name and role required"}), 400
    uid = re.sub(r'[^a-z0-9_]', '_', name.lower().replace(' ', '_'))
    if region:
        uid = uid + "_" + region.lower()
    if user_type == "sponsor":
        uid = "gs_" + role.lower() + "_" + uid
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO kpi_users (id, name, role, region, user_type, managed_role)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name=EXCLUDED.name, role=EXCLUDED.role, region=EXCLUDED.region,
                    user_type=EXCLUDED.user_type, managed_role=EXCLUDED.managed_role
            """, (uid, name, role, region, user_type, managed_role))
        conn.commit()
    return jsonify({"success": True, "id": uid})

@app.route("/admin/api/users/<uid>", methods=["DELETE"])
def admin_delete_user(uid):
    _require_admin()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kpi_users WHERE id=%s", (uid,))
        conn.commit()
    return jsonify({"success": True})

# ── Admin API: KPIs ───────────────────────────────────────────────────────────

@app.route("/admin/api/kpis", methods=["GET"])
def admin_get_kpis():
    _require_admin()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM kpi_definitions ORDER BY role, sort_order")
            kpis = [dict(k) for k in cur.fetchall()]
    return jsonify({"kpis": kpis})

@app.route("/admin/api/kpis", methods=["POST"])
def admin_add_kpi():
    _require_admin()
    data = request.json
    kid      = data.get("id", "").strip().replace(" ", "_")
    role     = data.get("role", "").strip()
    label    = data.get("label", "").strip()
    target   = data.get("target", "").strip()
    category = data.get("category", "").strip()
    if not kid or not role or not label:
        return jsonify({"error": "id, role, label required"}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO kpi_definitions (id, role, label, target, category, sort_order)
                SELECT %s, %s, %s, %s, %s, COALESCE(MAX(sort_order),0)+1
                FROM kpi_definitions WHERE role=%s
                ON CONFLICT (id, role) DO UPDATE SET
                    label=EXCLUDED.label, target=EXCLUDED.target, category=EXCLUDED.category
            """, (kid, role, label, target, category, role))
        conn.commit()
    return jsonify({"success": True})

@app.route("/admin/api/kpis/<kid>/<role>", methods=["DELETE"])
def admin_delete_kpi(kid, role):
    _require_admin()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kpi_definitions WHERE id=%s AND role=%s", (kid, role))
        conn.commit()
    return jsonify({"success": True})

@app.route("/admin/api/kpis/<kid>/<role>", methods=["PATCH"])
def admin_update_kpi(kid, role):
    _require_admin()
    data     = request.json
    label    = data.get("label", "").strip()
    target   = data.get("target", "").strip()
    category = data.get("category", "").strip()
    if not label:
        return jsonify({"error": "label required"}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE kpi_definitions SET label=%s, target=%s, category=%s
                WHERE id=%s AND role=%s
            """, (label, target, category, kid, role))
        conn.commit()
    return jsonify({"success": True})

# ── Admin API: Prior Month Data ───────────────────────────────────────────────

@app.route("/admin/api/prior", methods=["GET"])
def admin_get_prior():
    _require_admin()
    role   = request.args.get("role", "EA")
    region = request.args.get("region", "MEE")
    month  = request.args.get("month", "2026_08")
    kpis   = db_get_kpis(role)
    history = db_get_history(role, region, month)
    if isinstance(history, str):
        try:
            history = json.loads(history)
        except Exception:
            history = {}
    return jsonify({"kpis": [dict(k) for k in kpis], "history": history or {}})

@app.route("/admin/api/prior", methods=["POST"])
def admin_save_prior():
    _require_admin()
    data     = request.json
    role     = data.get("role", "EA")
    region   = data.get("region", "MEE")
    month    = data.get("month", "2026_08")
    kpi_data = data.get("kpi_data", {})
    hist_key = f"{role}_{region}_{month}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO kpi_history (hist_key, role, region, month, kpi_data)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (hist_key) DO UPDATE SET kpi_data=EXCLUDED.kpi_data
            """, (hist_key, role, region, month, json.dumps(kpi_data)))
        conn.commit()
    return jsonify({"success": True})

# ── Admin API: Submissions ────────────────────────────────────────────────────

@app.route("/admin/api/submissions", methods=["GET"])
def admin_get_submissions():
    _require_admin()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM kpi_submissions ORDER BY submitted_at DESC")
            subs = [dict(s) for s in cur.fetchall()]
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM kpi_definitions ORDER BY role, sort_order")
            all_kpis = cur.fetchall()
    kpis_by_role = {}
    for k in all_kpis:
        kpis_by_role.setdefault(k["role"], []).append(dict(k))
    # Ensure kpi_data is dict not string
    for s in subs:
        if isinstance(s.get("kpi_data"), str):
            try:
                s["kpi_data"] = json.loads(s["kpi_data"])
            except Exception:
                s["kpi_data"] = {}
        # Convert timestamps to string for JSON serialisation
        for ts_field in ("submitted_at", "approved_at"):
            if s.get(ts_field) and not isinstance(s[ts_field], str):
                s[ts_field] = s[ts_field].isoformat()
    return jsonify({"submissions": subs, "kpis_by_role": kpis_by_role})

@app.route("/health")
def health():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return jsonify({"status": "ok", "db": "connected"})
    except Exception as e:
        return jsonify({"status": "error", "db": str(e)}), 500

# ── Startup ───────────────────────────────────────────────────────────────────

try:
    init_db()
except Exception as e:
    log.error("DB init failed (will retry on first request): %s", e)

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
