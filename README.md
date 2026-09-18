# GTM26 KPI Conversational Collection Agent
### SAP IM Regional Strategy & Programs — Prototype v1.0

A conversational web agent that replaces the manual monthly KPI status update process for GTM26 Role Transformation. Regional Role Leaders are walked through each KPI one by one, shown last month's input, and asked for their update. Global Role Sponsors review all regional inputs and approve — triggering auto-generation of the PowerPoint report.

---

## Quick Start

### 1. Clone the repo

```bash
git clone https://github.wdf.sap.corp/I047384/kpi-agent.git
cd kpi-agent
```

### 2. Install dependencies

```bash
pip3 install flask
```

### 3. Run the app

```bash
python3 app.py
```

### 4. Open in browser

```
http://localhost:5000
```

---

## How to Use the Demo

### As a Regional Role Leader

1. Select your name from the login dropdown (e.g. **Dan Fagan — EA / MEE**)
2. Click **Continue**
3. Click **Begin Update →**
4. The agent walks you through each KPI — showing last month's narrative
5. Type your September update and press **Send** (or hit Enter)
6. Use **Skip this KPI** if needed
7. Once all KPIs are done, click **Submit to Sponsor ✓**

### As a Global Role Sponsor

1. Select a sponsor name (e.g. **Thomas Pfister — Global Sponsor EA**)
2. Click **Continue**
3. Review all 4 regional submissions using the region tabs
4. Click **Approve & Generate PPT ✓**

---

## Sample Logins

| Name | Type | Role / Region |
|---|---|---|
| Dan Fagan | Regional Leader | EA / MEE |
| Sara Mitchell | Regional Leader | EA / AMER |
| Raj Sharma | Regional Leader | DA / MEE |
| Tom Bradley | Regional Leader | DA / AMER |
| Mei Tanaka | Regional Leader | CEP / APAC |
| Alexander Sieferer | Regional Leader | CEP / EMEA |
| Thomas Pfister | Global Sponsor | EA (all regions) |
| Jan Manos | Global Sponsor | DA (all regions) |
| Bina Goleminova | Global Sponsor | CEP (all regions) |

---

## Project Structure

```
kpi-agent/
├── app.py                  # Flask backend — routes, session, logic
├── data/
│   ├── kpis.json           # KPI definitions per role
│   ├── stakeholders.json   # Regional leaders + global sponsors
│   ├── history.json        # Sample prior month data (Aug 2026)
│   └── db.json             # Live submissions + approvals (runtime)
└── templates/
    ├── login.html          # Login / user selection
    ├── agent.html          # Conversational KPI collection UI
    └── sponsor.html        # Sponsor approval dashboard
```

---

## Tech Stack

- **Backend:** Python 3.9 + Flask
- **Frontend:** Vanilla HTML / CSS / JavaScript
- **Storage:** JSON (prototype) → SAP BTP database (production)
- **Target hosting:** SAP BTP + AI Core

---

*Built by Subhankar Pattanayak — SAP IM Regional Strategy & Programs*
*Prototype for stakeholder review — September 2026*
