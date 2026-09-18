from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import json, os, uuid
from datetime import datetime

app = Flask(__name__)
app.secret_key = "kpi-agent-prototype-2026"

BASE = os.path.dirname(__file__)

def load(f):
    with open(os.path.join(BASE, "data", f)) as fh:
        return json.load(fh)

def save_db(db):
    with open(os.path.join(BASE, "data", "db.json"), "w") as fh:
        json.dump(db, fh, indent=2)

def get_db():
    return load("db.json")

def get_history_key(role, region, month=None):
    if not month:
        month = "2026_08"
    return f"{role}_{region}_{month}"

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    stakeholders = load("stakeholders.json")
    if request.method == "POST":
        uid = request.form.get("user_id")
        all_users = stakeholders["regional_leaders"] + stakeholders["global_sponsors"]
        user = next((u for u in all_users if u["id"] == uid), None)
        if user:
            session["user"] = user
            if any(u["id"] == uid for u in stakeholders["global_sponsors"]):
                session["role_type"] = "sponsor"
                return redirect(url_for("sponsor_dashboard"))
            else:
                session["role_type"] = "leader"
                return redirect(url_for("agent_start"))
    return render_template("login.html", stakeholders=stakeholders)

@app.route("/agent", methods=["GET"])
def agent_start():
    if "user" not in session:
        return redirect(url_for("login"))
    user = session["user"]
    kpi_data = load("kpis.json")
    role_info = kpi_data["roles"].get(user["role"], {})
    history = load("history.json")
    hist_key = get_history_key(user["role"], user["region"])
    prior = history.get(hist_key, {})
    session["kpis"] = role_info.get("kpis", [])
    session["kpi_index"] = 0
    session["responses"] = {}
    session["prior"] = prior
    return render_template("agent.html",
        user=user,
        role_name=role_info.get("name", user["role"]),
        total_kpis=len(role_info.get("kpis", [])),
        prior_month="August 2026"
    )

@app.route("/api/chat", methods=["POST"])
def chat():
    if "user" not in session:
        return jsonify({"error": "not logged in"}), 401

    data = request.json
    user_msg = data.get("message", "").strip()
    kpis = session.get("kpis", [])
    idx = session.get("kpi_index", 0)
    responses = session.get("responses", {})
    prior = session.get("prior", {})
    user = session["user"]

    # Greeting phase
    if idx == 0 and not user_msg:
        month_now = datetime.now().strftime("%B %Y")
        reply = {
            "type": "greeting",
            "text": (f"Hi {user['name'].split()[0]}! It's time for your monthly "
                     f"<strong>{session.get('role_name')} status update</strong> "
                     f"for <strong>{user['region']}</strong> — {month_now}.<br><br>"
                     f"I'll walk you through {len(kpis)} KPIs one by one. "
                     f"For each one I'll show what you said last month so you can update or confirm.<br><br>"
                     f"Ready to begin?"),
            "show_start": True
        }
        return jsonify(reply)

    # Start button clicked
    if user_msg.lower() in ["start", "yes", "let's go", "begin", "ready"]:
        session["kpi_index"] = 0
        idx = 0
        return jsonify(build_kpi_prompt(kpis, idx, prior))

    # Saving a KPI response
    if idx < len(kpis):
        kpi = kpis[idx]
        responses[kpi["id"]] = {
            "value": extract_value(user_msg),
            "narrative": user_msg,
            "timestamp": datetime.now().isoformat()
        }
        session["responses"] = responses
        session["kpi_index"] = idx + 1
        idx += 1

        if idx < len(kpis):
            return jsonify(build_kpi_prompt(kpis, idx, prior))
        else:
            # All KPIs done
            return jsonify({
                "type": "summary",
                "text": (f"All {len(kpis)} KPIs captured! Here's your summary:<br><br>" +
                         "".join([f"<b>{kpis[i]['label']}:</b> {responses[kpis[i]['id']]['narrative']}<br>"
                                  for i in range(len(kpis))]) +
                         "<br>Ready to submit to your Global Role Sponsor?"),
                "show_submit": True,
                "responses": responses
            })

    return jsonify({"type": "message", "text": "Please use the Submit button to finalise."})

def build_kpi_prompt(kpis, idx, prior):
    kpi = kpis[idx]
    prior_kpis = prior.get("kpis", {})
    prior_entry = prior_kpis.get(kpi["id"])
    prior_text = ""
    if prior_entry:
        prior_text = (f'<div class="prior-box">'
                      f'<span class="prior-label">Last month you said:</span><br>'
                      f'<em>"{prior_entry["narrative"]}"</em>'
                      f'</div>')
    return {
        "type": "kpi",
        "text": (f'<div class="kpi-header">'
                 f'<span class="kpi-num">KPI {idx+1} of {len(kpis)}</span> — '
                 f'<span class="kpi-name">{kpi["label"]}</span>'
                 f'<span class="kpi-target">Target: {kpi["target"]}</span>'
                 f'<span class="kpi-cat">{kpi["category"]}</span>'
                 f'</div>' +
                 prior_text +
                 f'<br>What\'s your update for this month?'),
        "kpi_id": kpi["id"],
        "kpi_index": idx,
        "total": len(kpis),
        "progress": round((idx / len(kpis)) * 100)
    }

def extract_value(text):
    import re
    m = re.search(r'(\d+\.?\d*\s*%)', text)
    return m.group(1) if m else "—"

@app.route("/api/submit", methods=["POST"])
def submit():
    if "user" not in session:
        return jsonify({"error": "not logged in"}), 401
    user = session["user"]
    responses = session.get("responses", {})
    kpis = session.get("kpis", [])
    db = get_db()
    sub_key = f"{user['role']}_{user['region']}_2026_09"
    db["submissions"][sub_key] = {
        "leader": user["name"],
        "role": user["role"],
        "region": user["region"],
        "month": "September 2026",
        "submitted_at": datetime.now().isoformat(),
        "status": "pending_approval",
        "kpis": responses
    }
    save_db(db)
    session.clear()
    return jsonify({
        "success": True,
        "text": (f"Submitted! Your {user['role']} status for {user['region']} "
                 f"has been sent to the Global Role Sponsor for review. "
                 f"You'll receive a confirmation once approved.")
    })

@app.route("/sponsor")
def sponsor_dashboard():
    if "user" not in session or session.get("role_type") != "sponsor":
        return redirect(url_for("login"))
    db = get_db()
    user = session["user"]
    kpi_data = load("kpis.json")
    stakeholders = load("stakeholders.json")
    history = load("history.json")

    # Gather all submissions for this sponsor's role
    role = user["role"] if "role" in user else None
    # Global sponsors manage one role each
    sponsor = next((s for s in stakeholders["global_sponsors"] if s["id"] == user["id"]), None)
    managed_role = sponsor["role"] if sponsor else None

    # Live submissions from db
    live_subs = {k: v for k, v in db["submissions"].items()
                 if v["role"] == managed_role} if managed_role else {}

    # Fill in historical sample data for regions not yet submitted
    regions = kpi_data["regions"]
    all_subs = {}
    for region in regions:
        live_key = f"{managed_role}_{region}_2026_09"
        hist_key = f"{managed_role}_{region}_2026_08"
        if live_key in live_subs:
            all_subs[region] = {"source": "live", "data": live_subs[live_key]}
        elif hist_key in history:
            all_subs[region] = {"source": "sample", "data": history[hist_key]}
        else:
            all_subs[region] = {"source": "pending", "data": None}

    role_info = kpi_data["roles"].get(managed_role, {})
    return render_template("sponsor.html",
        user=user,
        managed_role=managed_role,
        role_name=role_info.get("name", managed_role),
        kpis=role_info.get("kpis", []),
        regions=regions,
        all_subs=all_subs
    )

@app.route("/api/approve", methods=["POST"])
def approve():
    if "user" not in session:
        return jsonify({"error": "not logged in"}), 401
    data = request.json
    role = data.get("role")
    db = get_db()
    for key, val in db["submissions"].items():
        if val["role"] == role and val["status"] == "pending_approval":
            val["status"] = "approved"
            val["approved_at"] = datetime.now().isoformat()
    db["approvals"][role] = {
        "approved_by": session["user"]["name"],
        "approved_at": datetime.now().isoformat(),
        "role": role
    }
    save_db(db)
    return jsonify({"success": True, "message": f"All {role} submissions approved. PPT generation triggered."})

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(debug=True, port=5000)
