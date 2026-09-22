"""
KPI Agent — BTP Production Backend
Flask + PostgreSQL (shared prompt-db) + SAP AI Core
"""
import os, json, uuid, re, base64, logging, tempfile
from datetime import datetime
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
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

    CREATE TABLE IF NOT EXISTS kpi_sub_regions (
        id          SERIAL PRIMARY KEY,
        region      TEXT NOT NULL,
        sub_region  TEXT NOT NULL,
        sort_order  INT DEFAULT 0,
        UNIQUE (region, sub_region)
    );

    CREATE TABLE IF NOT EXISTS kpi_sub_submissions (
        sub_key      TEXT NOT NULL,
        region       TEXT NOT NULL,
        sub_region   TEXT NOT NULL,
        role         TEXT NOT NULL,
        leader_id    TEXT NOT NULL,
        leader_name  TEXT NOT NULL,
        month        TEXT NOT NULL,
        kpi_data     JSONB NOT NULL,
        submitted_at TIMESTAMP DEFAULT NOW(),
        PRIMARY KEY (sub_key, sub_region)
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

            # Sub-regions
            sub_regions = [
                ("APAC", "Japan",         1),
                ("APAC", "India",         2),
                ("APAC", "SEA",           3),
                ("APAC", "Korea",         4),
                ("APAC", "ANZ",           5),
                ("APAC", "Greater China", 6),
                ("EMEA", "UKI",           1),
                ("EMEA", "France",        2),
                ("EMEA", "Iberia",        3),
                ("EMEA", "Nordics",       4),
                ("EMEA", "Benelux",       5),
                ("EMEA", "South Africa",  6),
                ("EMEA", "MENA",          7),
                ("MEE",  "Germany",       1),
                ("MEE",  "Switzerland",   2),
                ("MEE",  "CEE",           3),
                ("AMER", "Latin America",            1),
                ("AMER", "US Midwest",               2),
                ("AMER", "US Northeast & Canada",    3),
                ("AMER", "US Public Services",       4),
                ("AMER", "US South",                 5),
                ("AMER", "US West",                  6),
            ]
            cur.executemany(
                "INSERT INTO kpi_sub_regions (region, sub_region, sort_order) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                sub_regions
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
    """Call AI Core LLM via orchestration deployment."""
    creds = get_aicore_creds()
    api_url = creds.get("serviceurls", {}).get("AI_API_URL", "")
    if not api_url:
        return None
    token = get_aicore_token()
    if not token:
        return None
    try:
        deployment_url = f"{api_url}/v2/inference/deployments/de2e0f366bd22cb0/completion"
        # Convert messages to orchestration templating format
        template = [{"role": m["role"], "content": m["content"]} for m in messages]
        payload = {
            "orchestration_config": {
                "module_configurations": {
                    "llm_module_config": {
                        "model_name": model,
                        "model_params": {"max_tokens": 512}
                    },
                    "templating_module_config": {
                        "template": template
                    }
                }
            },
            "input_params": {}
        }
        resp = requests.post(
            deployment_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "AI-Resource-Group": "default",
            },
            json=payload,
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        return data["orchestration_result"]["choices"][0]["message"]["content"]
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

def db_get_sub_regions(region):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT sub_region FROM kpi_sub_regions WHERE region=%s ORDER BY sort_order",
                (region,)
            )
            return [r["sub_region"] for r in cur.fetchall()]

def db_save_sub_submission(leader, role, region, sub_region, kpi_responses):
    sub_key = f"{role}_{region}_2026_09_{leader['id']}"
    log.info("Saving sub-submission: key=%s sub_region=%s kpis=%d", sub_key, sub_region, len(kpi_responses))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO kpi_sub_submissions
                    (sub_key, region, sub_region, role, leader_id, leader_name, month, kpi_data)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sub_key, sub_region) DO UPDATE SET
                    kpi_data = EXCLUDED.kpi_data,
                    submitted_at = NOW()
            """, (sub_key, region, sub_region, role, leader["id"], leader["name"],
                  "September 2026", json.dumps(kpi_responses)))
        conn.commit()
    log.info("Sub-submission saved OK: %s / %s", sub_key, sub_region)

def db_get_sub_submissions(role, region):
    """Return dict keyed by sub_region → kpi_data."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT sub_region, kpi_data, submitted_at
                FROM kpi_sub_submissions
                WHERE role=%s AND region=%s
                ORDER BY submitted_at DESC
            """, (role, region))
            rows = cur.fetchall()
    result = {}
    for r in rows:
        kd = r["kpi_data"]
        if isinstance(kd, str):
            try: kd = json.loads(kd)
            except Exception: kd = {}
        result[r["sub_region"]] = kd
    return result

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
        sub_regions = db_get_sub_regions(user["region"])
    except Exception as e:
        log.error("DB error on agent start: %s", e)
        kpis, prior, sub_regions = [], {}, []
    session["kpis"] = [dict(k) for k in kpis]
    session["kpi_index"] = 0
    session["sub_regions"] = sub_regions
    session["sub_region_index"] = 0
    session["responses"] = {}        # sub_region -> {kpi_id -> {value, narrative}}
    session["prior"] = dict(prior) if prior else {}
    role_labels = {"EA": "Enterprise Architect", "DA": "Digital Advisor", "CEP": "Customer Engagement Partner"}
    return render_template("agent.html",
        user=user,
        role_name=role_labels.get(user["role"], user["role"]),
        kpis=kpis,
        total_kpis=len(kpis),
        total_sub_regions=len(sub_regions),
        sub_regions=sub_regions,
        prior_month="August 2026"
    )

@app.route("/api/chat", methods=["POST"])
def chat():
    if "user" not in session:
        return jsonify({"error": "not logged in"}), 401

    data = request.json
    user_msg = (data.get("message") or "").strip()
    kpis = session.get("kpis", [])
    sub_regions = session.get("sub_regions", [])
    sri = session.get("sub_region_index", 0)   # which sub-region we're on
    idx = session.get("kpi_index", 0)           # which KPI within current sub-region
    responses = session.get("responses", {})    # {sub_region: {kpi_id: {...}}}
    prior = session.get("prior", {})
    user = session["user"]
    role_labels = {"EA": "Enterprise Architect", "DA": "Digital Advisor", "CEP": "Customer Engagement Partner"}
    role_name = role_labels.get(user["role"], user["role"])

    # ── Greeting ──────────────────────────────────────────────────────────────
    if sri == 0 and idx == 0 and not user_msg:
        sr_list = ", ".join(sub_regions) if sub_regions else user["region"]
        if sub_regions:
            return jsonify({
                "type": "greeting",
                "text": (f"Hi {user['name'].split()[0]}! Time for your <strong>{role_name} "
                         f"status update</strong> for <strong>{user['region']}</strong> — September 2026.<br><br>"
                         f"I'll collect <strong>{len(kpis)} KPIs</strong> for each of your "
                         f"<strong>{len(sub_regions)} sub-regions</strong>.<br><br>"
                         f"Choose a sub-region to start, or click <em>Begin in order</em> to go through them sequentially:"),
                "show_start": True,
                "sub_regions": sub_regions
            })
        return jsonify({
            "type": "greeting",
            "text": (f"Hi {user['name'].split()[0]}! Time for your <strong>{role_name} "
                     f"status update</strong> for <strong>{user['region']}</strong> — September 2026.<br><br>"
                     f"I'll collect <strong>{len(kpis)} KPIs</strong> for this region.<br><br>"
                     f"Ready to begin?"),
            "show_start": True
        })

    if user_msg.lower() in ["start", "yes", "begin", "ready", "let's go"]:
        # If we already have sub-regions and are on first sub-region, go straight to first KPI
        if sub_regions and sri == 0 and idx == 0:
            session["sub_region_index"] = 0
            session["kpi_index"] = 0
            return jsonify(_build_kpi_prompt(kpis, 0, prior,
                                             sub_region=sub_regions[0], sri=0, total_sr=len(sub_regions)))
        elif not sub_regions:
            session["kpi_index"] = 0
            return jsonify(_build_kpi_prompt(kpis, 0, prior))
        else:
            # Mid-flow "start" — just serve next KPI for current sub-region
            return jsonify(_build_kpi_prompt(kpis, idx, prior,
                                             sub_region=sub_regions[sri] if sri < len(sub_regions) else None,
                                             sri=sri, total_sr=len(sub_regions)))

    if user_msg.lower() == "start_sr":
        # "Start <sub-region> →" button clicked — serve first KPI for current sub-region
        current_sr = sub_regions[sri] if sub_regions and sri < len(sub_regions) else None
        if current_sr:
            session["kpi_index"] = 0
            return jsonify(_build_kpi_prompt(kpis, 0, prior,
                                             sub_region=current_sr, sri=sri, total_sr=len(sub_regions)))
        elif not sub_regions:
            session["kpi_index"] = 0
            return jsonify(_build_kpi_prompt(kpis, 0, prior))
        return jsonify({"type": "message", "text": "No sub-regions to start."})

    if user_msg.lower().startswith("jump_sr:"):
        # User selected a specific sub-region to work on
        try:
            jump_idx = int(user_msg.split(":")[1])
        except (IndexError, ValueError):
            jump_idx = 0
        if sub_regions and 0 <= jump_idx < len(sub_regions):
            target_sr = sub_regions[jump_idx]
            already_done = target_sr in responses and len(responses[target_sr]) >= len(kpis)
            # If fully done, restart from scratch (editing mode)
            if already_done:
                responses[target_sr] = {}
                session["responses"] = responses
            done_count = len(responses.get(target_sr, {}))
            resume_kpi = done_count if done_count < len(kpis) else 0
            session["sub_region_index"] = jump_idx
            session["kpi_index"] = resume_kpi
            return jsonify(_build_kpi_prompt(kpis, resume_kpi, prior,
                                             sub_region=target_sr, sri=jump_idx,
                                             total_sr=len(sub_regions)))

    if user_msg.lower().startswith("edit_kpi:"):
        # "edit_kpi:sri:kpi_index" — re-open a specific already-answered KPI for editing
        try:
            parts = user_msg.split(":")
            edit_sri  = int(parts[1])
            edit_kidx = int(parts[2])
        except (IndexError, ValueError):
            return jsonify({"type": "message", "text": "Invalid edit request."})
        if sub_regions and 0 <= edit_sri < len(sub_regions):
            target_sr = sub_regions[edit_sri]
            session["sub_region_index"] = edit_sri
            session["kpi_index"] = edit_kidx
            session["is_editing"] = True
            return jsonify(_build_kpi_prompt(kpis, edit_kidx, prior,
                                             sub_region=target_sr, sri=edit_sri,
                                             total_sr=len(sub_regions),
                                             is_edit=True))
        elif not sub_regions:
            session["kpi_index"] = edit_kidx if 0 <= edit_kidx < len(kpis) else 0
            session["is_editing"] = True
            return jsonify(_build_kpi_prompt(kpis, session["kpi_index"], prior, is_edit=True))
        return jsonify({"type": "message", "text": "Cannot navigate to that KPI."})


    if not sub_regions:
        return _flat_chat(user_msg, kpis, idx, responses, prior, user, role_name)

    # ── Sub-region collection flow ────────────────────────────────────────────
    current_sr = sub_regions[sri] if sri < len(sub_regions) else None

    if current_sr is None:
        return jsonify({"type": "message", "text": "All sub-regions complete. Please submit."})

    sr_responses = responses.get(current_sr, {})

    if idx < len(kpis):
        kpi = kpis[idx]
        kpi_id    = kpi["id"]     if isinstance(kpi, dict) else kpi[0]
        kpi_label = kpi["label"]  if isinstance(kpi, dict) else kpi[2]
        kpi_tgt   = kpi["target"] if isinstance(kpi, dict) else kpi[3]

        prior_entry = prior.get(kpi_id, {})
        prior_narrative = prior_entry.get("narrative", "") if prior_entry else ""
        prior_value     = prior_entry.get("value", "")     if prior_entry else ""

        # Split "VALUE||NARRATIVE" combined message from value+narrative inputs
        explicit_value = None
        if "||" in user_msg:
            parts = user_msg.split("||", 1)
            explicit_value = parts[0].strip()
            user_msg_for_ai = parts[1].strip() if parts[1].strip() else explicit_value
        else:
            user_msg_for_ai = user_msg

        if user_msg.upper() == "[SKIP]":
            resolved_narrative = "[Skipped]"
            resolved_value = "—"
            ai_ack = ""
        else:
            resolved_narrative, ai_ack = _ai_interpret_kpi_response(
                kpi_label=kpi_label, kpi_target=kpi_tgt,
                user_msg=user_msg_for_ai,
                prior_narrative=prior_narrative, prior_value=prior_value,
                explicit_value=explicit_value,
            )
            resolved_value = explicit_value if explicit_value else _extract_value(resolved_narrative)

        sr_responses[kpi_id] = {
            "value": resolved_value,
            "narrative": resolved_narrative,
            "timestamp": datetime.now().isoformat()
        }
        responses[current_sr] = sr_responses
        session["responses"] = responses

        # Save to DB immediately on every KPI answer — no data loss on session drop
        try:
            db_save_sub_submission(user, user["role"], user["region"], current_sr, sr_responses)
        except Exception as e:
            log.error("Per-KPI sub-submission save error: %s", e)

        is_editing = session.pop("is_editing", False)
        next_kpi_idx = idx + 1
        session["kpi_index"] = next_kpi_idx

        reply_base = {"acknowledgement": ai_ack} if ai_ack else {}
        reply_base["confirmed_value"] = resolved_value
        if resolved_narrative.strip().lower() != user_msg_for_ai.strip().lower():
            reply_base["resolved_narrative"] = resolved_narrative

        # If this was an edit, ask what next rather than auto-advancing (save already done above)
        if is_editing:
            next_unanswered = next((i for i, k in enumerate(kpis)
                                    if k["id"] not in sr_responses or not sr_responses[k["id"]].get("value")), None)
            if next_unanswered is not None:
                session["kpi_index"] = next_unanswered
                reply = _build_kpi_prompt(kpis, next_unanswered, prior,
                                          sub_region=current_sr, sri=sri, total_sr=len(sub_regions))
                reply.update(reply_base)
                return jsonify(reply)
            else:
                session["kpi_index"] = len(kpis)
                resp = {"type": "summary",
                        "text": f"All {len(kpis)} KPIs for <strong>{current_sr}</strong> are now up to date.",
                        "show_submit": True}
                resp.update(reply_base)
                return jsonify(resp)

        if next_kpi_idx < len(kpis):
            # More KPIs in this sub-region
            reply = _build_kpi_prompt(kpis, next_kpi_idx, prior,
                                      sub_region=current_sr, sri=sri, total_sr=len(sub_regions))
            reply.update(reply_base)
            return jsonify(reply)
        else:
            # Sub-region done — already saved per-KPI above
            next_sri = sri + 1
            session["sub_region_index"] = next_sri
            session["kpi_index"] = 0

            if next_sri < len(sub_regions):
                # Move to next sub-region
                next_sr = sub_regions[next_sri]
                intro = _build_sub_region_intro(next_sr, kpis, prior,
                                                sri=next_sri, total_sr=len(sub_regions),
                                                done_sr=current_sr)
                intro.update(reply_base)
                return jsonify(intro)
            else:
                # All sub-regions done — build regional roll-up from sub-region data
                agg = {}
                # Collect all values per KPI across sub-regions
                kpi_values = {}   # kid -> list of (sub_region, value, narrative)
                for sr, sr_data in responses.items():
                    for kid, entry in sr_data.items():
                        kpi_values.setdefault(kid, []).append((sr, entry.get("value",""), entry.get("narrative","")))

                for kid, entries in kpi_values.items():
                    # Try to compute average for % KPIs
                    pcts = []
                    for _, val, _ in entries:
                        m = re.search(r'(\d+(?:\.\d+)?)\s*%', str(val))
                        if m:
                            pcts.append(float(m.group(1)))
                    if pcts and len(pcts) == len(entries):
                        avg = round(sum(pcts) / len(pcts), 1)
                        agg_value = f"{avg}%"
                        sr_parts = ", ".join(f"{sr}: {val}" for sr, val, _ in entries)
                        agg_narrative = f"Regional average {avg}% (sub-regions — {sr_parts}). See sub-region breakdown for details."
                    else:
                        # Non-% KPI — list all sub-region values
                        sr_parts = "; ".join(f"{sr}: {val}" for sr, val, _ in entries if val)
                        agg_value = entries[0][1] if entries else ""
                        agg_narrative = f"Sub-region summary — {sr_parts}. See sub-region breakdown for details." if sr_parts else (entries[0][2] if entries else "")
                    agg[kid] = {
                        "value": agg_value,
                        "narrative": agg_narrative,
                        "timestamp": datetime.now().isoformat()
                    }
                try:
                    db_save_submission(user, user["role"], user["region"], agg)
                except Exception as e:
                    log.error("Final submit error: %s", e)

                kpi_labels = {
                    (k["id"] if isinstance(k, dict) else k[0]): (k["label"] if isinstance(k, dict) else k[2])
                    for k in kpis
                }
                sr_summary = "".join(
                    f"<b>{sr}:</b> {len(sr_data)} KPIs captured<br>"
                    for sr, sr_data in responses.items()
                )
                final = {
                    "type": "summary",
                    "text": (f"All <strong>{len(sub_regions)} sub-regions</strong> complete!<br><br>"
                             f"{sr_summary}<br>"
                             f"Your {user['role']} updates for <strong>{user['region']}</strong> "
                             f"are ready to submit to your Global Role Sponsor."),
                    "show_submit": True
                }
                final.update(reply_base)
                return jsonify(final)

    return jsonify({"type": "message", "text": "Please use the Submit button to finalise."})

def _ai_interpret_kpi_response(kpi_label, kpi_target, user_msg, prior_narrative, prior_value, explicit_value=None):
    """
    Use AI Core (low-cost model) to resolve what the user typed into a proper narrative.

    Key behaviours:
    - "change it to 83%" / "update to 78%" → patch the prior sentence, replacing the old figure
    - "same" / "no change" → return prior narrative verbatim
    - Short bare number/% with no prior → expand into a full sentence
    - Full sentence → use as-is
    Returns (resolved_narrative, ack_sentence).
    """
    prior_context = ""
    if prior_narrative:
        prior_context = (
            f'\nLast month narrative: "{prior_narrative}"'
            + (f' (value captured: {prior_value})' if prior_value else "")
        )

    system_prompt = (
        "You are a KPI narrative assistant for a regional sales leader. "
        "Given a KPI name, its target, the prior month narrative, and what the user just typed, "
        "return a JSON object with exactly two keys:\n"
        "  \"narrative\": the final sentence/text to record for this KPI this month.\n"
        "  \"ack\": one short, friendly confirmation sentence (≤12 words) to show the user.\n\n"
        "Rules — follow strictly:\n"
        "1. SAME-AS-LAST: If the user says 'same', 'same as last month', 'no change', 'unchanged', "
        "   'copy', 'reuse', 'keep' — return the exact prior month narrative as the narrative.\n"
        "2. DELTA-UPDATE: If the user gives only a number/% change (e.g. 'change it to 83%', "
        "   '83%', 'update to 78', 'now 91%', 'it's 77 now') — take the prior month narrative "
        "   and replace ONLY the percentage/number figure in it with the new value the user gave. "
        "   Keep all other wording of the sentence identical. If there's no prior narrative, "
        "   build a natural sentence using the KPI name, target and the number given.\n"
        "3. FULL SENTENCE: If the user provides a complete sentence or detailed update, use it verbatim.\n"
        "4. SHORT PHRASE (not a number): Expand it into a full sentence using the KPI context.\n"
        "5. Never invent facts not in the user's input or the prior narrative.\n"
        "6. Return ONLY valid JSON. No markdown fences, no extra text outside the JSON.\n\n"
        'Example — delta: prior="Joule activation reached 79% this month, behind the 90% target.", '
        'user says "change it to 83%" → '
        '{"narrative": "Joule activation reached 83% this month, behind the 90% target.", '
        '"ack": "Updated to 83% — noted!"}'
    )

    value_context = f'\nExplicit KPI value entered: {explicit_value}' if explicit_value else ""

    user_prompt = (
        f"KPI: {kpi_label}\n"
        f"Target: {kpi_target}"
        f"{prior_context}"
        f"{value_context}\n"
        f'User typed: "{user_msg}"'
    )

    # Use gpt-4o for interpretation
    raw = ai_core_chat([
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ], model="gpt-4o")

    if raw:
        try:
            cleaned = re.sub(r"^```json\s*|```$", "", raw.strip(), flags=re.MULTILINE).strip()
            parsed = json.loads(cleaned)
            narrative = (parsed.get("narrative") or "").strip() or user_msg
            ack = (parsed.get("ack") or "").strip()
            return narrative, ack
        except Exception:
            # try regex extraction as fallback
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                try:
                    parsed = json.loads(m.group())
                    narrative = (parsed.get("narrative") or "").strip() or user_msg
                    ack = (parsed.get("ack") or "").strip()
                    return narrative, ack
                except Exception:
                    pass

    # ── Hard fallback (no AI Core / parse failure) ────────────────────────────
    same_phrases = ["same", "same as last month", "no change", "use last month",
                    "copy", "reuse", "keep same", "as before", "unchanged"]
    if user_msg.lower().strip() in same_phrases or any(p in user_msg.lower() for p in same_phrases):
        return (prior_narrative if prior_narrative else user_msg), "Reusing last month's update."

    # Delta fallback — if user gave just a %, patch prior narrative manually
    delta_match = re.search(r'(\d+(?:\.\d+)?)\s*%', user_msg)
    if delta_match and prior_narrative:
        new_pct = delta_match.group(0)
        patched = re.sub(r'\d+(?:\.\d+)?\s*%', new_pct, prior_narrative, count=1)
        return patched, f"Updated to {new_pct}."

    return user_msg, ""


def _build_sub_region_intro(sub_region, kpis, prior, sri=0, total_sr=1, done_sr=None):
    done_msg = f"<em>{done_sr} saved.</em><br><br>" if done_sr else ""
    return {
        "type": "sub_region_start",
        "text": (f"{done_msg}"
                 f'<div class="sr-header">'
                 f'Sub-region <strong>{sri+1} of {total_sr}</strong>: '
                 f'<strong>{sub_region}</strong>'
                 f'</div>'
                 f"I'll now collect <strong>{len(kpis)} KPIs</strong> for <strong>{sub_region}</strong>. "
                 f"Ready?"),
        "sub_region": sub_region,
        "sri": sri,
        "total_sr": total_sr,
        "show_start_sr": True
    }

def _build_kpi_prompt(kpis, idx, prior, sub_region=None, sri=None, total_sr=None, is_edit=False):
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

    sr_label = f'<span class="sr-badge">{sub_region}</span> ' if sub_region else ""
    sr_progress = ""
    if sri is not None and total_sr:
        sr_progress = f'<span class="sr-progress">Sub-region {sri+1}/{total_sr}</span> · '

    edit_notice = " (editing — your previous answer is shown above)" if is_edit else ""
    return {
        "type": "kpi",
        "text": (f'<div class="kpi-header">'
                 f'{sr_progress}{sr_label}'
                 f'<span class="kpi-num">KPI {idx+1} of {total}</span> — '
                 f'<span class="kpi-name">{kpi_label}</span> '
                 f'<span class="kpi-target">Target: {kpi_tgt}</span> '
                 f'<span class="kpi-cat">{kpi_cat}</span>'
                 f'</div>' + prior_html +
                 f"<br>What's your update for <strong>{sub_region or 'this month'}</strong>?{edit_notice}"),
        "kpi_id": kpi_id,
        "kpi_index": idx,
        "total": total,
        "sub_region": sub_region,
        "sri": sri,
        "progress": round((idx / total) * 100),
        "show_value_input": True,
        "kpi_target": kpi_tgt,
        "is_edit": is_edit,
    }

def _flat_chat(user_msg, kpis, idx, responses, prior, user, role_name):
    """Fallback chat handler for leaders with no sub-regions defined."""
    if idx < len(kpis):
        kpi = kpis[idx]
        kpi_id    = kpi["id"]    if isinstance(kpi, dict) else kpi[0]
        kpi_label = kpi["label"] if isinstance(kpi, dict) else kpi[2]
        kpi_tgt   = kpi["target"] if isinstance(kpi, dict) else kpi[3]
        prior_entry = prior.get(kpi_id, {})
        prior_narrative = prior_entry.get("narrative", "") if prior_entry else ""
        prior_value     = prior_entry.get("value", "")     if prior_entry else ""

        # Split "VALUE||NARRATIVE" combined message
        explicit_value = None
        if "||" in user_msg:
            parts = user_msg.split("||", 1)
            explicit_value = parts[0].strip()
            user_msg_for_ai = parts[1].strip() if parts[1].strip() else explicit_value
        else:
            user_msg_for_ai = user_msg

        if user_msg.upper() == "[SKIP]":
            resolved_narrative, ai_ack = "[Skipped]", ""
            resolved_value = "—"
        else:
            resolved_narrative, ai_ack = _ai_interpret_kpi_response(
                kpi_label=kpi_label, kpi_target=kpi_tgt,
                user_msg=user_msg_for_ai, prior_narrative=prior_narrative, prior_value=prior_value,
                explicit_value=explicit_value,
            )
            resolved_value = explicit_value if explicit_value else _extract_value(resolved_narrative)
        responses[kpi_id] = {"value": resolved_value,
                              "narrative": resolved_narrative,
                              "timestamp": datetime.now().isoformat()}
        session["responses"] = responses
        session["kpi_index"] = idx + 1
        next_idx = idx + 1
        reply_base = {}
        if ai_ack: reply_base["acknowledgement"] = ai_ack
        reply_base["confirmed_value"] = resolved_value
        if resolved_narrative.strip().lower() != user_msg_for_ai.strip().lower():
            reply_base["resolved_narrative"] = resolved_narrative
        if next_idx < len(kpis):
            reply = _build_kpi_prompt(kpis, next_idx, prior)
            reply.update(reply_base)
            return jsonify(reply)
        else:
            db_save_submission(user, user["role"], user["region"], responses)
            resp = {"type": "summary",
                    "text": f"All {len(kpis)} KPIs captured! Ready to submit?",
                    "show_submit": True}
            resp.update(reply_base)
            return jsonify(resp)
    return jsonify({"type": "message", "text": "Please submit."})

def _extract_value(text):
    m = re.search(r'(\d+\.?\d*\s*%|\$[\d,.]+[KMB]?|\d+\.\d+|\d+)', text)
    return m.group(1) if m else "—"

@app.route("/api/submit", methods=["POST"])
def submit():
    if "user" not in session:
        return jsonify({"error": "not logged in"}), 401
    user = session["user"]
    responses = session.get("responses", {})
    sub_regions = session.get("sub_regions", [])
    try:
        if sub_regions:
            # Save each sub-region individually first
            for sr, sr_data in responses.items():
                if sr_data:
                    try:
                        db_save_sub_submission(user, user["role"], user["region"], sr, sr_data)
                    except Exception as e:
                        log.error("Sub-submission save error (%s): %s", sr, e)

            # Build proper aggregated roll-up (same logic as chat flow)
            kpi_values = {}
            for sr, sr_data in responses.items():
                for kid, entry in sr_data.items():
                    kpi_values.setdefault(kid, []).append((sr, entry.get("value", ""), entry.get("narrative", "")))

            agg = {}
            for kid, entries in kpi_values.items():
                pcts = []
                for _, val, _ in entries:
                    m = re.search(r'(\d+(?:\.\d+)?)\s*%', str(val))
                    if m:
                        pcts.append(float(m.group(1)))
                if pcts and len(pcts) == len(entries):
                    avg = round(sum(pcts) / len(pcts), 1)
                    sr_parts = ", ".join(f"{sr}: {val}" for sr, val, _ in entries)
                    agg[kid] = {
                        "value": f"{avg}%",
                        "narrative": f"Regional average {avg}% (sub-regions — {sr_parts}). See sub-region breakdown for details.",
                        "timestamp": datetime.now().isoformat()
                    }
                else:
                    sr_parts = "; ".join(f"{sr}: {val}" for sr, val, _ in entries if val)
                    agg[kid] = {
                        "value": entries[0][1] if entries else "",
                        "narrative": f"Sub-region summary — {sr_parts}. See sub-region breakdown for details." if sr_parts else (entries[0][2] if entries else ""),
                        "timestamp": datetime.now().isoformat()
                    }
            db_save_submission(user, user["role"], user["region"], agg)
        else:
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

@app.route("/api/sub-submissions/<role>/<region>", methods=["GET"])
def get_sub_submissions(role, region):
    if "user" not in session:
        return jsonify({"error": "not logged in"}), 401
    try:
        sub_regions = db_get_sub_regions(region)
        sub_data = db_get_sub_submissions(role, region)
        kpis = [dict(k) for k in db_get_kpis(role)]
    except Exception as e:
        log.error("Sub-submissions API error: %s", e)
        return jsonify({"error": str(e)}), 500
    return jsonify({
        "sub_regions": sub_regions,
        "sub_data": sub_data,
        "kpis": kpis
    })

@app.route("/api/my-kpis", methods=["GET"])
def my_kpis():
    """Return the logged-in user's current KPI entries from DB, keyed by sub_region → kpi_id → {value, narrative}."""
    if "user" not in session:
        return jsonify({"error": "not logged in"}), 401
    user = session["user"]
    try:
        sub_regions = db_get_sub_regions(user["region"])
        sub_data    = db_get_sub_submissions(user["role"], user["region"])
        kpis        = [dict(k) for k in db_get_kpis(user["role"])]
    except Exception as e:
        log.error("my-kpis error: %s", e)
        return jsonify({"sub_regions": [], "sub_data": {}, "kpis": []}), 200
    return jsonify({"sub_regions": sub_regions, "sub_data": sub_data, "kpis": kpis})

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

# ── Admin API: Sub-regions ────────────────────────────────────────────────────

@app.route("/admin/api/sub-regions", methods=["GET"])
def admin_get_sub_regions():
    _require_admin()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM kpi_sub_regions ORDER BY region, sort_order")
            rows = [dict(r) for r in cur.fetchall()]
    # Group by region
    grouped = {}
    for r in rows:
        grouped.setdefault(r["region"], []).append(r)
    return jsonify({"sub_regions": grouped})

@app.route("/admin/api/sub-regions", methods=["POST"])
def admin_add_sub_region():
    _require_admin()
    data = request.json
    region     = data.get("region", "").strip().upper()
    sub_region = data.get("sub_region", "").strip()
    if not region or not sub_region:
        return jsonify({"error": "region and sub_region required"}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO kpi_sub_regions (region, sub_region, sort_order)
                SELECT %s, %s, COALESCE(MAX(sort_order),0)+1
                FROM kpi_sub_regions WHERE region=%s
                ON CONFLICT (region, sub_region) DO NOTHING
            """, (region, sub_region, region))
        conn.commit()
    return jsonify({"success": True})

@app.route("/admin/api/sub-regions/<int:sid>", methods=["DELETE"])
def admin_delete_sub_region(sid):
    _require_admin()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kpi_sub_regions WHERE id=%s", (sid,))
        conn.commit()
    return jsonify({"success": True})

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

@app.route("/api/generate-ppt", methods=["POST"])
def generate_ppt():
    if "user" not in session or session.get("role_type") != "sponsor":
        return jsonify({"error": "not authorised"}), 401
    user = session["user"]
    managed_role = user.get("managed_role") or user.get("role")

    try:
        from ppt_generator import generate_role_ppt
    except ImportError as e:
        return jsonify({"error": "ppt_generator not available: " + str(e)}), 500

    try:
        regions = db_get_regions()
        kpis = [dict(k) for k in db_get_kpis(managed_role)]
        live_subs = db_get_submissions(managed_role)
    except Exception as e:
        log.error("PPT DB error: %s", e)
        return jsonify({"error": "DB error"}), 500

    # Build submissions dict keyed by region (same logic as sponsor_dashboard)
    live_by_region = {}
    for s in live_subs:
        region = s["region"] if isinstance(s, dict) else s[5]
        live_by_region[region] = dict(s) if isinstance(s, dict) else {
            "region": s[5], "status": s[6], "kpi_data": s[7]
        }

    submissions = {}
    for region in regions:
        if region in live_by_region:
            row = live_by_region[region]
            raw = row.get("kpi_data", {})
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    raw = {}
            submissions[region] = {"kpi_data": raw, "status": row.get("status", "")}
        else:
            hist = db_get_history(managed_role, region)
            if hist:
                if isinstance(hist, str):
                    try:
                        hist = json.loads(hist)
                    except Exception:
                        hist = {}
                submissions[region] = {"kpi_data": hist, "status": "sample"}

    month_label = "September 2026"
    tmp = tempfile.NamedTemporaryFile(
        suffix=".pptx",
        delete=False,
        prefix="KPI_RoleUpdate_{}_".format(managed_role)
    )
    tmp.close()
    try:
        generate_role_ppt(
            role=managed_role,
            sponsor_name=user.get("name", ""),
            month_label=month_label,
            submissions=submissions,
            kpis=kpis,
            output_path=tmp.name
        )
    except Exception as e:
        log.error("PPT generation failed: %s", e)
        return jsonify({"error": "PPT generation failed: " + str(e)}), 500

    filename = "KPI_RoleUpdate_{}_Sep2026.pptx".format(managed_role)
    return send_file(
        tmp.name,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )


@app.route("/health")
def health():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return jsonify({"status": "ok", "db": "connected"})
    except Exception as e:
        return jsonify({"status": "error", "db": str(e)}), 500

@app.route("/admin/reseed-subregions", methods=["POST"])
def reseed_subregions():
    """One-shot: create tables and seed sub-regions. Admin only."""
    if not session.get("is_admin"):
        from flask import abort
        abort(401)
    sub_regions = [
        ("APAC","Japan",1),("APAC","India",2),("APAC","SEA",3),
        ("APAC","Korea",4),("APAC","ANZ",5),("APAC","Greater China",6),
        ("EMEA","UKI",1),("EMEA","France",2),("EMEA","Iberia",3),
        ("EMEA","Nordics",4),("EMEA","Benelux",5),("EMEA","South Africa",6),("EMEA","MENA",7),
        ("MEE","Germany",1),("MEE","Switzerland",2),("MEE","CEE",3),
        ("AMER","Latin America",1),("AMER","US Midwest",2),
        ("AMER","US Northeast & Canada",3),("AMER","US Public Services",4),
        ("AMER","US South",5),("AMER","US West",6),
    ]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS kpi_sub_regions (
                    id SERIAL PRIMARY KEY,
                    region TEXT NOT NULL,
                    sub_region TEXT NOT NULL,
                    sort_order INT DEFAULT 0,
                    UNIQUE (region, sub_region)
                );
                CREATE TABLE IF NOT EXISTS kpi_sub_submissions (
                    sub_key TEXT NOT NULL,
                    region TEXT NOT NULL,
                    sub_region TEXT NOT NULL,
                    role TEXT NOT NULL,
                    leader_id TEXT NOT NULL,
                    leader_name TEXT NOT NULL,
                    month TEXT NOT NULL,
                    kpi_data JSONB NOT NULL,
                    submitted_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (sub_key, sub_region)
                );
            """)
            cur.executemany(
                "INSERT INTO kpi_sub_regions (region, sub_region, sort_order) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                sub_regions
            )
        conn.commit()
    return jsonify({"success": True, "seeded": len(sub_regions)})

# ── Startup ───────────────────────────────────────────────────────────────────

try:
    init_db()
except Exception as e:
    log.error("DB init failed (will retry on first request): %s", e)

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
