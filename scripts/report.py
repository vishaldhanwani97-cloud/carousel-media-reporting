"""
Carousel Media — Daily Meta Ads Report
Runs every morning at 8am IST via GitHub Actions
"""

import os
import json
import base64
import requests
import anthropic
import gspread
import pytz
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google.oauth2 import service_account
import jinja2
import weasyprint

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

IST = pytz.timezone("Asia/Kolkata")
TODAY = datetime.now(IST).date()
YESTERDAY = TODAY - timedelta(days=1)
META_BASE = "https://graph.facebook.com/v19.0"

META_TOKEN = os.environ["META_ACCESS_TOKEN"]
CLAUDE_KEY = os.environ["CLAUDE_API_KEY"]
GMAIL_CLIENT_ID = os.environ["GMAIL_CLIENT_ID"]
GMAIL_CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]
GMAIL_REFRESH_TOKEN = os.environ["GMAIL_REFRESH_TOKEN"]
GMAIL_SENDER = os.environ["GMAIL_SENDER"]
TRELLO_KEY = os.environ["TRELLO_API_KEY"]
TRELLO_TOKEN = os.environ["TRELLO_TOKEN"]
TRELLO_BOARD_ID = os.environ["TRELLO_BOARD_ID"]
SHEETS_ID = os.environ["GOOGLE_SHEETS_ID"]
SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT"]

claude = anthropic.Anthropic(api_key=CLAUDE_KEY)


# ─── GOOGLE SHEETS CONFIG ─────────────────────────────────────────────────────

def get_sheets_client():
    sa_info = json.loads(SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return gspread.authorize(creds)


def load_config():
    """Load all config from Google Sheets."""
    gc = get_sheets_client()
    sh = gc.open_by_key(SHEETS_ID)

    # Sheet 1: Accounts
    accounts_ws = sh.worksheet("Accounts")
    accounts_data = accounts_ws.get_all_records()
    accounts = [a for a in accounts_data if str(a.get("Active", "")).upper() == "Y"]

    # Sheet 2: Thresholds
    thresh_ws = sh.worksheet("Thresholds")
    thresholds = {r["Account Name"]: r for r in thresh_ws.get_all_records()}

    # Sheet 3: Prompts
    prompts_ws = sh.worksheet("Prompts")
    prompts = {r["Prompt Name"]: r["Prompt Text"]
               for r in prompts_ws.get_all_records()
               if str(r.get("Active", "")).upper() == "Y"}

    # Sheet 4: Team
    team_ws = sh.worksheet("Team")
    team = team_ws.get_all_records()

    return accounts, thresholds, prompts, team


# ─── META API ─────────────────────────────────────────────────────────────────

def meta_get(endpoint, params={}):
    params["access_token"] = META_TOKEN
    r = requests.get(f"{META_BASE}/{endpoint}", params=params)
    r.raise_for_status()
    return r.json()


def get_account_insights(account_id, date_preset):
    """Pull campaign-level insights for a given date preset."""
    fields = (
        "campaign_name,campaign_id,status,objective,"
        "spend,impressions,clicks,ctr,cpc,cpm,reach,"
        "actions,action_values,cost_per_action_type,"
        "frequency"
    )
    data = meta_get(f"act_{account_id}/insights", {
        "level": "campaign",
        "date_preset": date_preset,
        "fields": fields,
        "limit": 100
    })
    return data.get("data", [])


def get_all_insights(account_id):
    """Pull yesterday, L7D, L14D, L30D for one account."""
    return {
        "yesterday": get_account_insights(account_id, "yesterday"),
        "last_7d": get_account_insights(account_id, "last_7d"),
        "last_14d": get_account_insights(account_id, "last_14d"),
        "last_30d": get_account_insights(account_id, "last_30d"),
    }


def extract_purchases(actions, key="offsite_conversion.fb_pixel_purchase"):
    """Extract purchase count from actions array."""
    if not actions:
        return 0
    for a in actions:
        if a.get("action_type") == key:
            return float(a.get("value", 0))
    return 0


def extract_revenue(action_values, key="offsite_conversion.fb_pixel_purchase"):
    """Extract purchase revenue from action_values array."""
    if not action_values:
        return 0
    for a in action_values:
        if a.get("action_type") == key:
            return float(a.get("value", 0))
    return 0


def compute_account_summary(insights_by_window):
    """Compute account-level aggregates across windows."""
    summary = {}
    for window, campaigns in insights_by_window.items():
        spend = sum(float(c.get("spend", 0)) for c in campaigns)
        revenue = sum(extract_revenue(c.get("action_values", [])) for c in campaigns)
        purchases = sum(extract_purchases(c.get("actions", [])) for c in campaigns)
        impressions = sum(int(c.get("impressions", 0)) for c in campaigns)
        clicks = sum(int(c.get("clicks", 0)) for c in campaigns)
        roas = round(revenue / spend, 2) if spend > 0 else 0
        ctr = round((clicks / impressions * 100), 2) if impressions > 0 else 0
        cpm = round((spend / impressions * 1000), 2) if impressions > 0 else 0
        cpc = round(spend / clicks, 2) if clicks > 0 else 0
        summary[window] = {
            "spend": round(spend, 2),
            "revenue": round(revenue, 2),
            "purchases": int(purchases),
            "roas": roas,
            "ctr": ctr,
            "cpm": cpm,
            "cpc": cpc,
            "impressions": impressions,
            "clicks": clicks,
            "active_campaigns": len([c for c in campaigns if float(c.get("spend", 0)) > 0])
        }
    return summary


# ─── CLAUDE ANALYSIS ──────────────────────────────────────────────────────────

def analyse_account(account, insights, account_summary, thresholds, prompts):
    """Send account data to Claude and get decisions."""
    thresh = thresholds.get(account["Account Name"], {})
    overview_prompt = prompts.get("overview_insights", "")

    payload = {
        "account_name": account["Account Name"],
        "account_id": account["Meta Account ID"],
        "goals": {
            "roas_goal": float(thresh.get("ROAS Goal", 2.0)),
            "cac_goal": float(thresh.get("CAC Goal", 500)),
            "max_frequency": float(thresh.get("Max Frequency", 3.5)),
        },
        "thresholds": {
            "roas_min": float(thresh.get("ROAS Min", 1.5)),
            "cpm_increase_pct": float(thresh.get("CPM Max % Increase", 25)),
            "cpc_increase_pct": float(thresh.get("CPC Max % Increase", 20)),
            "ctr_drop_pct": float(thresh.get("CTR Drop % Alert", 30)),
            "min_purchases_for_decision": int(thresh.get("Min Purchases", 10)),
        },
        "account_avg": account_summary.get("last_30d", {}),
        "windows": {
            "yesterday": account_summary.get("yesterday", {}),
            "last_7d": account_summary.get("last_7d", {}),
            "last_14d": account_summary.get("last_14d", {}),
            "last_30d": account_summary.get("last_30d", {}),
        },
        "campaigns": {
            "yesterday": insights.get("yesterday", []),
            "last_7d": insights.get("last_7d", []),
        }
    }

    prompt = overview_prompt.replace("{{OVERVIEW_DATA}}", json.dumps(payload, indent=2))
    prompt += "\n\nSEASONALITY_CONTEXT: No major seasonal event active today."

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        return {"insights": [], "error": str(e)}


def detect_anomalies(account_name, summary, thresholds):
    """Detect threshold breaches and flag them."""
    alerts = []
    thresh = thresholds.get(account_name, {})

    l7d = summary.get("last_7d", {})
    l14d = summary.get("last_14d", {})
    l30d = summary.get("last_30d", {})

    roas_goal = float(thresh.get("ROAS Goal", 2.0))
    cpm_max_pct = float(thresh.get("CPM Max % Increase", 25)) / 100
    cpc_max_pct = float(thresh.get("CPC Max % Increase", 20)) / 100
    ctr_drop_pct = float(thresh.get("CTR Drop % Alert", 30)) / 100
    roas_min = float(thresh.get("ROAS Min", 1.5))

    # ROAS below minimum
    if l7d.get("roas", 0) < roas_min and l7d.get("spend", 0) > 0:
        alerts.append({
            "type": "roas_low",
            "severity": "high" if l7d["roas"] < roas_min * 0.8 else "medium",
            "message": f"ROAS {l7d['roas']}x is below minimum threshold {roas_min}x",
            "metric": "ROAS",
            "value": l7d["roas"],
            "threshold": roas_min
        })

    # CPM spike vs 30D baseline
    if l30d.get("cpm", 0) > 0 and l7d.get("cpm", 0) > 0:
        cpm_change = (l7d["cpm"] - l30d["cpm"]) / l30d["cpm"]
        if cpm_change > cpm_max_pct:
            alerts.append({
                "type": "cpm_spike",
                "severity": "medium",
                "message": f"CPM up {round(cpm_change*100)}% vs 30D avg (₹{l7d['cpm']} vs ₹{l30d['cpm']})",
                "metric": "CPM",
                "value": l7d["cpm"],
                "threshold": l30d["cpm"] * (1 + cpm_max_pct)
            })

    # CPC spike vs 30D baseline
    if l30d.get("cpc", 0) > 0 and l7d.get("cpc", 0) > 0:
        cpc_change = (l7d["cpc"] - l30d["cpc"]) / l30d["cpc"]
        if cpc_change > cpc_max_pct:
            alerts.append({
                "type": "cpc_spike",
                "severity": "medium",
                "message": f"CPC up {round(cpc_change*100)}% vs 30D avg (₹{l7d['cpc']} vs ₹{l30d['cpc']})",
                "metric": "CPC",
                "value": l7d["cpc"],
                "threshold": l30d["cpc"] * (1 + cpc_max_pct)
            })

    # CTR drop vs 30D baseline
    if l30d.get("ctr", 0) > 0 and l7d.get("ctr", 0) > 0:
        ctr_change = (l30d["ctr"] - l7d["ctr"]) / l30d["ctr"]
        if ctr_change > ctr_drop_pct:
            alerts.append({
                "type": "ctr_drop",
                "severity": "medium",
                "message": f"CTR dropped {round(ctr_change*100)}% vs 30D avg ({l7d['ctr']}% vs {l30d['ctr']}%)",
                "metric": "CTR",
                "value": l7d["ctr"],
                "threshold": l30d["ctr"] * (1 - ctr_drop_pct)
            })

    return alerts


# ─── TRELLO ───────────────────────────────────────────────────────────────────

def get_trello_list_id(list_name="To Do"):
    """Get the ID of a Trello list by name."""
    r = requests.get(
        f"https://api.trello.com/1/boards/{TRELLO_BOARD_ID}/lists",
        params={"key": TRELLO_KEY, "token": TRELLO_TOKEN}
    )
    r.raise_for_status()
    for lst in r.json():
        if lst["name"] == list_name:
            return lst["id"]
    return None


def create_trello_card(list_id, title, description, label_color="red"):
    """Create a Trello card."""
    r = requests.post(
        "https://api.trello.com/1/cards",
        params={"key": TRELLO_KEY, "token": TRELLO_TOKEN},
        json={
            "idList": list_id,
            "name": title,
            "desc": description,
            "due": datetime.now(IST).strftime("%Y-%m-%dT23:59:00.000Z")
        }
    )
    r.raise_for_status()
    return r.json()


def create_tasks_in_trello(all_results, team):
    """Create Trello cards for all flagged actions."""
    todo_list_id = get_trello_list_id("To Do")
    if not todo_list_id:
        print("Could not find To Do list in Trello")
        return

    date_str = TODAY.strftime("%d %b %Y")

    for result in all_results:
        account_name = result["account_name"]
        owner_email = result["account"]["Owner"]
        owner_name = next((t["Name"] for t in team if t["Email"] == owner_email), owner_email)

        # Cards from Claude insights
        for insight in result.get("claude_insights", {}).get("insights", []):
            if insight.get("type") in ["fix", "scale"]:
                title = f"{account_name} — {insight.get('title', 'Action needed')} | {date_str}"
                desc = f"""**Account:** {account_name}
**Owner:** {owner_name}
**Type:** {insight.get('type', '').upper()}
**Level:** {insight.get('level', '')}

**Insight:**
{insight.get('text', '')}

**Entity:** {insight.get('entity_name', 'Account level')}

---
*Auto-generated by Carousel Media Reporting System*
*{date_str}*"""
                create_trello_card(todo_list_id, title, desc)

        # Cards from anomaly alerts
        for alert in result.get("alerts", []):
            if alert["severity"] == "high":
                title = f"{account_name} — {alert['metric']} Alert | {date_str}"
                desc = f"""**Account:** {account_name}
**Owner:** {owner_name}
**Alert Type:** {alert['type'].replace('_', ' ').title()}
**Severity:** {alert['severity'].upper()}

**Details:**
{alert['message']}

**Action Required:** Investigate immediately and update this card with findings.

---
*Auto-generated by Carousel Media Reporting System*
*{date_str}*"""
                create_trello_card(todo_list_id, title, desc)


# ─── HTML/PDF REPORT ──────────────────────────────────────────────────────────

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 13px;
         color: #1a1a1a; background: #fff; padding: 32px; }
  .header { border-bottom: 3px solid #1a1a1a; padding-bottom: 16px; margin-bottom: 24px;
            display: flex; justify-content: space-between; align-items: flex-end; }
  .header-title { font-size: 22px; font-weight: 700; letter-spacing: -0.5px; }
  .header-sub { font-size: 12px; color: #666; margin-top: 4px; }
  .header-date { font-size: 12px; color: #666; text-align: right; }
  .section-title { font-size: 11px; font-weight: 700; letter-spacing: .08em;
                   text-transform: uppercase; color: #888; margin: 24px 0 12px; }
  .exec-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 24px; }
  .exec-card { background: #f7f7f7; border-radius: 6px; padding: 14px 16px; }
  .exec-card .label { font-size: 10px; color: #888; text-transform: uppercase;
                      letter-spacing: .06em; margin-bottom: 6px; }
  .exec-card .value { font-size: 20px; font-weight: 700; }
  .exec-card .sub { font-size: 10px; color: #888; margin-top: 3px; }
  .green { color: #1a7a4a; } .red { color: #c0392b; } .amber { color: #d68910; }
  .accounts-table { width: 100%; border-collapse: collapse; margin-bottom: 24px; }
  .accounts-table th { font-size: 10px; font-weight: 700; text-transform: uppercase;
                       letter-spacing: .06em; color: #888; text-align: left;
                       padding: 8px 10px; border-bottom: 1.5px solid #e0e0e0; }
  .accounts-table td { padding: 9px 10px; border-bottom: 1px solid #f0f0f0;
                       font-size: 12px; vertical-align: middle; }
  .accounts-table tr:hover td { background: #fafafa; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
           font-size: 10px; font-weight: 600; }
  .badge-green { background: #e8f5ef; color: #1a7a4a; }
  .badge-red { background: #fdecea; color: #c0392b; }
  .badge-amber { background: #fef9e7; color: #d68910; }
  .badge-gray { background: #f0f0f0; color: #666; }
  .alert-block { background: #fdecea; border-left: 3px solid #c0392b;
                 padding: 10px 14px; border-radius: 0 6px 6px 0; margin-bottom: 8px; }
  .alert-block .alert-title { font-weight: 700; font-size: 12px; margin-bottom: 3px; }
  .alert-block .alert-body { font-size: 11px; color: #555; }
  .insight-block { background: #f7f9ff; border-left: 3px solid #2980b9;
                   padding: 10px 14px; border-radius: 0 6px 6px 0; margin-bottom: 8px; }
  .insight-block.scale { border-color: #1a7a4a; background: #e8f5ef; }
  .insight-block.fix { border-color: #c0392b; background: #fdecea; }
  .insight-block.watch { border-color: #d68910; background: #fef9e7; }
  .insight-title { font-weight: 700; font-size: 12px; margin-bottom: 3px; }
  .insight-body { font-size: 11px; color: #444; }
  .task-section { margin-bottom: 20px; }
  .task-owner { font-size: 13px; font-weight: 700; margin-bottom: 8px;
                padding-bottom: 6px; border-bottom: 1px solid #e0e0e0; }
  .task-item { display: flex; gap: 10px; padding: 8px 0;
               border-bottom: 1px solid #f5f5f5; font-size: 12px; }
  .task-account { font-weight: 600; min-width: 140px; }
  .task-action { color: #444; flex: 1; }
  .task-deadline { font-size: 10px; color: #888; white-space: nowrap; }
  .footer { margin-top: 32px; padding-top: 12px; border-top: 1px solid #e0e0e0;
            font-size: 10px; color: #aaa; text-align: center; }
  .roas-bar { height: 5px; background: #e0e0e0; border-radius: 3px;
              display: inline-block; width: 60px; vertical-align: middle; margin-left: 6px; }
  .roas-fill { height: 5px; border-radius: 3px; }
  .divider { border: none; border-top: 1px solid #e0e0e0; margin: 20px 0; }
  .no-alerts { color: #888; font-size: 12px; font-style: italic; padding: 8px 0; }
</style>
</head>
<body>

<div class="header">
  <div>
    <div class="header-title">Carousel Media</div>
    <div class="header-sub">Daily Performance Report</div>
  </div>
  <div class="header-date">
    {{ report_date }}<br>
    <span style="color:#aaa">Generated at 8:00 AM IST</span>
  </div>
</div>

<!-- EXECUTIVE SUMMARY -->
<div class="section-title">Executive Summary</div>
<div class="exec-grid">
  <div class="exec-card">
    <div class="label">Active Clients</div>
    <div class="value">{{ exec.active_clients }}</div>
    <div class="sub">of {{ exec.total_clients }} total</div>
  </div>
  <div class="exec-card">
    <div class="label">Total Spend (Yesterday)</div>
    <div class="value">₹{{ exec.total_spend_yesterday }}</div>
    <div class="sub">₹{{ exec.total_spend_7d }} last 7D</div>
  </div>
  <div class="exec-card">
    <div class="label">Blended ROAS (7D)</div>
    <div class="value {% if exec.blended_roas_7d >= 2.0 %}green{% elif exec.blended_roas_7d >= 1.5 %}amber{% else %}red{% endif %}">
      {{ exec.blended_roas_7d }}×
    </div>
    <div class="sub">{{ exec.blended_roas_mtd }}× MTD</div>
  </div>
  <div class="exec-card">
    <div class="label">Accounts on Track</div>
    <div class="value green">{{ exec.on_track }}</div>
    <div class="sub">{{ exec.needs_attention }} need attention</div>
  </div>
  <div class="exec-card">
    <div class="label">Alerts Today</div>
    <div class="value {% if exec.total_alerts > 3 %}red{% elif exec.total_alerts > 0 %}amber{% else %}green{% endif %}">
      {{ exec.total_alerts }}
    </div>
    <div class="sub">{{ exec.high_alerts }} high priority</div>
  </div>
</div>

<!-- ACCOUNTS SNAPSHOT -->
<div class="section-title">All Accounts — Yesterday | Last 7D | MTD</div>
<table class="accounts-table">
  <thead>
    <tr>
      <th>Account</th>
      <th>Owner</th>
      <th>Spend (Y)</th>
      <th>Spend (7D)</th>
      <th>ROAS (7D)</th>
      <th>CPM (7D)</th>
      <th>CPC (7D)</th>
      <th>CTR (7D)</th>
      <th>Purchases (7D)</th>
      <th>Status</th>
    </tr>
  </thead>
  <tbody>
    {% for row in account_rows %}
    <tr>
      <td style="font-weight:600">{{ row.name }}</td>
      <td>{{ row.owner_name }}</td>
      <td>₹{{ row.spend_yesterday }}</td>
      <td>₹{{ row.spend_7d }}</td>
      <td>
        <span class="{% if row.roas_7d >= row.roas_goal %}green{% elif row.roas_7d >= row.roas_goal * 0.8 %}amber{% else %}red{% endif %}">
          {{ row.roas_7d }}×
        </span>
        <span class="roas-bar">
          <span class="roas-fill" style="width:{{ [row.roas_7d / row.roas_goal * 100, 100]|min }}%;
            background:{% if row.roas_7d >= row.roas_goal %}#1a7a4a{% elif row.roas_7d >= row.roas_goal*0.8 %}#d68910{% else %}#c0392b{% endif %}">
          </span>
        </span>
      </td>
      <td>₹{{ row.cpm_7d }}</td>
      <td>₹{{ row.cpc_7d }}</td>
      <td>{{ row.ctr_7d }}%</td>
      <td>{{ row.purchases_7d }}</td>
      <td>
        {% if row.status == 'on_track' %}
          <span class="badge badge-green">On Track</span>
        {% elif row.status == 'watch' %}
          <span class="badge badge-amber">Watch</span>
        {% elif row.status == 'alert' %}
          <span class="badge badge-red">Alert</span>
        {% else %}
          <span class="badge badge-gray">No Data</span>
        {% endif %}
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table>

<hr class="divider">

<!-- ALERTS & ANOMALIES -->
<div class="section-title">Alerts & Anomalies</div>
{% if all_alerts %}
  {% for alert_group in all_alerts %}
    {% for alert in alert_group.alerts %}
    <div class="alert-block">
      <div class="alert-title">{{ alert_group.account }} — {{ alert.metric }} Alert</div>
      <div class="alert-body">{{ alert.message }} · Owner: {{ alert_group.owner_name }}</div>
    </div>
    {% endfor %}
  {% endfor %}
{% else %}
  <div class="no-alerts">No threshold breaches detected today. All accounts within normal range.</div>
{% endif %}

<hr class="divider">

<!-- CLAUDE INSIGHTS -->
<div class="section-title">AI-Powered Insights</div>
{% for result in all_results %}
  {% if result.claude_insights.insights %}
  <div style="margin-bottom: 16px;">
    <div style="font-weight:700; font-size:12px; margin-bottom:6px; color:#333;">
      {{ result.account_name }}
    </div>
    {% for insight in result.claude_insights.insights %}
    <div class="insight-block {{ insight.type }}">
      <div class="insight-title">
        [{{ insight.type|upper }}] {{ insight.title }}
      </div>
      <div class="insight-body">{{ insight.text }}</div>
    </div>
    {% endfor %}
  </div>
  {% endif %}
{% endfor %}

<hr class="divider">

<!-- TASKS BY OWNER -->
<div class="section-title">Today's Tasks by Team Member</div>
{% for owner_group in tasks_by_owner %}
<div class="task-section">
  <div class="task-owner">{{ owner_group.name }} — {{ owner_group.role }}</div>
  {% if owner_group.tasks %}
    {% for task in owner_group.tasks %}
    <div class="task-item">
      <div class="task-account">{{ task.account }}</div>
      <div class="task-action">{{ task.action }}</div>
      <div class="task-deadline">{{ task.deadline }}</div>
    </div>
    {% endfor %}
  {% else %}
    <div class="no-alerts">No tasks assigned today.</div>
  {% endif %}
</div>
{% endfor %}

<div class="footer">
  Carousel Media Daily Report · {{ report_date }} · Auto-generated at 8:00 AM IST<br>
  Powered by Claude AI · Do not reply to this email
</div>

</body>
</html>
"""


def build_report_data(all_results, team, thresholds):
    """Assemble all data needed for the HTML template."""
    date_str = TODAY.strftime("%A, %d %B %Y")

    # Executive summary aggregates
    total_spend_y = 0
    total_spend_7d = 0
    total_revenue_7d = 0
    total_revenue_mtd = 0
    total_spend_mtd = 0
    on_track = 0
    needs_attention = 0
    all_alerts = []
    total_alerts = 0
    high_alerts = 0

    account_rows = []
    for result in all_results:
        s = result["summary"]
        account = result["account"]
        thresh = thresholds.get(account["Account Name"], {})
        roas_goal = float(thresh.get("ROAS Goal", 2.0))
        owner_name = next((t["Name"] for t in team if t["Email"] == account.get("Owner", "")), account.get("Owner", ""))

        y = s.get("yesterday", {})
        l7 = s.get("last_7d", {})
        mtd = s.get("last_30d", {})  # using 30d as MTD proxy

        total_spend_y += y.get("spend", 0)
        total_spend_7d += l7.get("spend", 0)
        total_revenue_7d += l7.get("revenue", 0)
        total_revenue_mtd += mtd.get("revenue", 0)
        total_spend_mtd += mtd.get("spend", 0)

        roas_7d = l7.get("roas", 0)
        if l7.get("spend", 0) == 0:
            status = "no_data"
        elif roas_7d >= roas_goal:
            status = "on_track"
            on_track += 1
        elif roas_7d >= roas_goal * 0.8:
            status = "watch"
            needs_attention += 1
        else:
            status = "alert"
            needs_attention += 1

        account_rows.append({
            "name": account["Account Name"],
            "owner_name": owner_name,
            "spend_yesterday": f"{y.get('spend', 0):,.0f}",
            "spend_7d": f"{l7.get('spend', 0):,.0f}",
            "roas_7d": roas_7d,
            "roas_goal": roas_goal,
            "cpm_7d": f"{l7.get('cpm', 0):,.0f}",
            "cpc_7d": f"{l7.get('cpc', 0):,.0f}",
            "ctr_7d": l7.get("ctr", 0),
            "purchases_7d": l7.get("purchases", 0),
            "status": status
        })

        if result.get("alerts"):
            alert_entry = {
                "account": account["Account Name"],
                "owner_name": owner_name,
                "alerts": result["alerts"]
            }
            all_alerts.append(alert_entry)
            total_alerts += len(result["alerts"])
            high_alerts += len([a for a in result["alerts"] if a["severity"] == "high"])

    blended_roas_7d = round(total_revenue_7d / total_spend_7d, 2) if total_spend_7d > 0 else 0
    blended_roas_mtd = round(total_revenue_mtd / total_spend_mtd, 2) if total_spend_mtd > 0 else 0

    exec_summary = {
        "active_clients": len([r for r in all_results if r["summary"].get("yesterday", {}).get("spend", 0) > 0]),
        "total_clients": len(all_results),
        "total_spend_yesterday": f"{total_spend_y:,.0f}",
        "total_spend_7d": f"{total_spend_7d:,.0f}",
        "blended_roas_7d": blended_roas_7d,
        "blended_roas_mtd": blended_roas_mtd,
        "on_track": on_track,
        "needs_attention": needs_attention,
        "total_alerts": total_alerts,
        "high_alerts": high_alerts
    }

    # Tasks by owner
    owner_task_map = {}
    for result in all_results:
        account_name = result["account_name"]
        owner_email = result["account"].get("Owner", "")
        owner_name = next((t["Name"] for t in team if t["Email"] == owner_email), owner_email)
        role = next((t.get("Role", "") for t in team if t["Email"] == owner_email), "")

        if owner_email not in owner_task_map:
            owner_task_map[owner_email] = {"name": owner_name, "role": role, "tasks": []}

        for insight in result.get("claude_insights", {}).get("insights", []):
            if insight.get("type") in ["fix", "scale"]:
                owner_task_map[owner_email]["tasks"].append({
                    "account": account_name,
                    "action": insight.get("text", "")[:120],
                    "deadline": "Today EOD"
                })

        for alert in result.get("alerts", []):
            owner_task_map[owner_email]["tasks"].append({
                "account": account_name,
                "action": alert["message"],
                "deadline": "Immediate" if alert["severity"] == "high" else "Today EOD"
            })

    tasks_by_owner = list(owner_task_map.values())

    return {
        "report_date": date_str,
        "exec": exec_summary,
        "account_rows": account_rows,
        "all_alerts": all_alerts,
        "all_results": all_results,
        "tasks_by_owner": tasks_by_owner
    }


def generate_pdf(report_data):
    """Render HTML template and convert to PDF."""
    env = jinja2.Environment()
    env.filters["min"] = min
    template = env.from_string(HTML_TEMPLATE)
    html_content = template.render(**report_data)

    pdf_path = f"/tmp/carousel_report_{TODAY.strftime('%Y%m%d')}.pdf"
    weasyprint.HTML(string=html_content).write_pdf(pdf_path)
    return pdf_path


# ─── EMAIL ────────────────────────────────────────────────────────────────────

def get_gmail_service():
    """Authenticate Gmail using OAuth2."""
    creds = Credentials(
        token=None,
        refresh_token=GMAIL_REFRESH_TOKEN,
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token"
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def send_email(pdf_path, report_data, team):
    """Send the daily report email with PDF attachment."""
    service = get_gmail_service()

    recipients = [t["Email"] for t in team if t.get("Email")]
    date_str = TODAY.strftime("%d %b %Y")

    alert_count = report_data["exec"]["total_alerts"]
    subject_flag = f"⚠️ {alert_count} Alert{'s' if alert_count != 1 else ''} — " if alert_count > 0 else "✅ All Good — "
    subject = f"Carousel Media Daily Report | {subject_flag}{date_str}"

    # Plain text body for email preview
    body_text = f"""
Carousel Media Daily Performance Report — {date_str}

EXECUTIVE SUMMARY
━━━━━━━━━━━━━━━━
Active Clients: {report_data['exec']['active_clients']} of {report_data['exec']['total_clients']}
Total Spend (Yesterday): ₹{report_data['exec']['total_spend_yesterday']}
Total Spend (Last 7D): ₹{report_data['exec']['total_spend_7d']}
Blended ROAS (7D): {report_data['exec']['blended_roas_7d']}x
Accounts on Track: {report_data['exec']['on_track']}
Needs Attention: {report_data['exec']['needs_attention']}
Alerts Today: {report_data['exec']['total_alerts']} ({report_data['exec']['high_alerts']} high priority)

Please see the attached PDF for the full report including account snapshots,
AI insights, alerts, and today's tasks.

TASKS SUMMARY
━━━━━━━━━━━━━
"""
    for owner_group in report_data["tasks_by_owner"]:
        if owner_group["tasks"]:
            body_text += f"\n{owner_group['name']} ({owner_group['role']}):\n"
            for task in owner_group["tasks"]:
                body_text += f"  • [{task['account']}] {task['action'][:100]} — {task['deadline']}\n"

    body_text += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Carousel Media Reporting System
Auto-generated at 8:00 AM IST
All tasks have been created in Trello → https://trello.com/b/{TRELLO_BOARD_ID}
"""

    msg = MIMEMultipart()
    msg["From"] = GMAIL_SENDER
    msg["To"] = recipients[0]
    msg["Cc"] = ", ".join(recipients[1:]) if len(recipients) > 1 else ""
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain"))

    # Attach PDF
    with open(pdf_path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f"attachment; filename=carousel_report_{TODAY.strftime('%Y%m%d')}.pdf"
        )
        msg.attach(part)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"✅ Email sent to {', '.join(recipients)}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print(f"🚀 Starting Carousel Media Daily Report — {TODAY}")

    # 1. Load config from Google Sheets
    print("📊 Loading config from Google Sheets...")
    accounts, thresholds, prompts, team = load_config()
    print(f"   Loaded {len(accounts)} active accounts, {len(team)} team members")

    # 2. Pull Meta data + analyse each account
    all_results = []
    for account in accounts:
        account_id = str(account["Meta Account ID"]).replace("act_", "")
        account_name = account["Account Name"]
        print(f"   Pulling data for {account_name}...")

        try:
            insights = get_all_insights(account_id)
            summary = compute_account_summary(insights)
            alerts = detect_anomalies(account_name, summary, thresholds)

            print(f"   Analysing {account_name} with Claude...")
            claude_insights = analyse_account(account, insights, summary, thresholds, prompts)

            all_results.append({
                "account_name": account_name,
                "account": account,
                "insights": insights,
                "summary": summary,
                "alerts": alerts,
                "claude_insights": claude_insights
            })
        except Exception as e:
            print(f"   ⚠️ Error processing {account_name}: {e}")
            all_results.append({
                "account_name": account_name,
                "account": account,
                "insights": {},
                "summary": {},
                "alerts": [],
                "claude_insights": {"insights": [], "error": str(e)}
            })

    # 3. Create Trello cards
    print("📋 Creating Trello cards...")
    try:
        create_tasks_in_trello(all_results, team)
    except Exception as e:
        print(f"   ⚠️ Trello error: {e}")

    # 4. Build report data + generate PDF
    print("📄 Generating PDF report...")
    report_data = build_report_data(all_results, team, thresholds)
    pdf_path = generate_pdf(report_data)
    print(f"   PDF saved to {pdf_path}")

    # 5. Send email
    print("📧 Sending email...")
    send_email(pdf_path, report_data, team)

    print("✅ Done!")


if __name__ == "__main__":
    main()
