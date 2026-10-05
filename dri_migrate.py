import os, json, psycopg2

def get_db_creds():
    raw = os.environ.get("VCAP_SERVICES", "{}")
    svcs = json.loads(raw)
    for svc_list in svcs.values():
        for svc in svc_list:
            if svc.get("name") == "prompt-db":
                return svc["credentials"]
    raise RuntimeError("prompt-db not found in VCAP_SERVICES")

c = get_db_creds()
conn = psycopg2.connect(
    host=c["hostname"], port=int(c["port"]),
    dbname=c["dbname"], user=c["username"],
    password=c["password"], sslmode="require"
)
cur = conn.cursor()

cur.execute("ALTER TABLE kpi_users ADD COLUMN IF NOT EXISTS sub_region TEXT")

dri_users = [
    ("dri_markus_bauer",  "Markus Bauer",  "EA", "MEE", "dri", None, "Germany"),
    ("dri_sophie_klein",  "Sophie Klein",  "EA", "MEE", "dri", None, "Switzerland"),
    ("dri_felix_wagner",  "Felix Wagner",  "EA", "MEE", "dri", None, "CEE"),
]
for u in dri_users:
    cur.execute(
        "INSERT INTO kpi_users (id, name, role, region, user_type, managed_role, sub_region) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        u
    )

dri_kpis = [
    ("dri_joule_activated",     "DRI_EA", "Joule Activated",              "90%",  "Business Impact", 1),
    ("dri_ai_adoption_plan",    "DRI_EA", "AI Adoption Plan",             "100%", "Business Impact", 2),
    ("dri_rwsm_dashboard",      "DRI_EA", "RwSM Dashboard Activated",     "80%",  "Business Impact", 3),
    ("dri_risk_assessment",     "DRI_EA", "Risk Assessment Available",    "100%", "Business Impact", 4),
    ("dri_calm_tenant",         "DRI_EA", "CALM Tenant Activated",        "95%",  "Tool Adoption",   5),
    ("dri_gainsight_usage",     "DRI_EA", "Gainsight Usage",              "85%",  "Tool Adoption",   6),
    ("dri_signavio_usage",      "DRI_EA", "Active Signavio Usage",        "75%",  "Tool Adoption",   7),
    ("dri_leanix_usage",        "DRI_EA", "Active LeanIX Usage",          "75%",  "Tool Adoption",   8),
    ("dri_ea_coverage_cust",    "DRI_EA", "EA Coverage (# of Customers)", "95%",  "Coverage",        9),
    ("dri_ea_coverage_acv",     "DRI_EA", "EA Coverage (Exit ACV)",       ">89%", "Coverage",        10),
    ("dri_ea_customer_density", "DRI_EA", "EA Customer Density",          "2.5+", "Coverage",        11),
    ("dri_ea_acv_density",      "DRI_EA", "EA ACV Density",               "",     "Coverage",        12),
    ("dri_leanix_enablement",   "DRI_EA", "Lean IX Enablement",           "90%",  "Learning",        13),
    ("dri_ea_learning_journey", "DRI_EA", "EA Learning Journey",          "95%",  "Learning",        14),
]
for k in dri_kpis:
    cur.execute(
        "INSERT INTO kpi_definitions (id, role, label, target, category, sort_order) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        k
    )

cur.execute("SELECT id, name, user_type, sub_region FROM kpi_users WHERE user_type='dri'")
print("DRI users:", cur.fetchall())
cur.execute("SELECT COUNT(*) FROM kpi_definitions WHERE role='DRI_EA'")
print("DRI_EA KPIs:", cur.fetchone()[0])

conn.commit()
cur.close()
conn.close()
print("Migration complete!")
