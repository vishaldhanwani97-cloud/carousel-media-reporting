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
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

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
TRELLO_TOKEN_VAL = os.environ["TRELLO_TOKEN"]
TRELLO_BOARD_ID = os.environ["TRELLO_BOARD_ID"]
SHEETS_ID = os.environ["GOOGLE_SHEETS_ID"]

claude = anthropic.Anthropic(api_key=CLAUDE_KEY)


def get_sheets_client():
    creds = Credentials(
        token=None,
        refresh_token=GMAIL_REFRESH_TOKEN,
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/spreadsheets.readonly"
        ]
    )
    creds.refresh(Request())
    return gspread.authorize(creds)


def load_config():
    gc = get_sheets_client()
    sh = gc.open_by_key(SHEETS_ID)
    accounts = [a for a in sh.worksheet("Accounts").get_all_records()
                if str(a.get("Active", "")).upper() == "Y"]
    thresholds = {r["Account Name"]: r
                  for r in sh.worksheet("Thresholds").get_all_records()}
    prompts = {r["Prompt Name"]: r["Prompt Text"]
               for r in sh.worksheet("Prompts").get_all_records()
               if str(r.get("Active", "")).upper() == "Y"}
    team = sh.worksheet("Team").get_all_records()
    return accounts, thresholds, prompts, team


def meta_get(endpoint, params={}):
    params["access_token"] = META_TOKEN
    r = requests.get(f"{META_BASE}/{endpoint}", params=params)
    r.raise_for_status()
    return r.json()


def get_account_insights(account_id, date_preset):
    fields = (
        "campaign_name,campaign_id,"
        "spend,impressions,clicks,ctr,cpc,cpm,reach,"
        "actions,action_values,frequency"
    )
    data = meta_get(f"act_{account_id}/insights", {
        "level": "campaign",
        "date_preset": date_preset,
        "fields": fields,
        "limit": 100
    })
    return data.get("data", [])


def get_all_insights(account_id):
    return {
        "yesterday": get_account_insights(account_id, "yesterday"),
        "last_7d": get_account_insights(account_id, "last_7d"),
        "last_14d": get_account_insights(account_id, "last_14d"),
        "last_30d": get_account_insights(account_id, "last_30d"),
    }


def extract_purchases(actions):
    if not actions:
        return 0
    for a in actions:
        if a.get("action_type") == "offsite_conversion.fb_pixel_purchase":
            return float(a.get("value", 0))
    return 0


def extract_revenue(action_values):
    if not action_values:
        return 0
    for a in action_values:
        if a.get("action_type") == "offsite_conversion.fb_pixel_purchase":
            return float(a.get("value", 0))
    return 0


def compute_account_summary(insights_by_window):
    summary = {}
    for window, campaigns in insights_by_window.items():
        spend = sum(float(c.get("spend", 0)) for c in campaigns)
        revenue = sum(extract_revenue(c.get("action_values", [])) for c in campaigns)
        purchases = sum(extract_purchases(c.get("actions", [])) for c in campaigns)
        impressions = sum(int(c.get("impressions", 0)) for c in campaigns)
        clicks = sum(int(c.get("clicks", 0)) for c in campaigns)
        summary[window] = {
            "spend": round(spend, 2),
            "revenue": round(revenue, 2),
            "purchases": int(purchases),
            "roas": round(revenue / spend, 2) if spend > 0 else 0,
            "ctr": round(clicks / impressions * 100, 2) if impressions > 0 else 0,
            "cpm": round(spend / impressions * 1000, 2) if impressions > 0 else 0,
            "cpc": round(spend / clicks, 2) if clicks > 0 else 0,
            "impressions": impressions,
            "clicks": clicks,
        }
    return summary


def analyse_account(account, insights, summary, thresholds, prompts):
    thresh = thresholds.get(account["Account Name"], {})
    prompt = prompts.get("overview_insights", "")
    payload = {
        "account_name": account["Account Name"],
        "goals": {
            "roas_goal": float(thresh.get("ROAS Goal", 2.0)),
            "cac_goal": float(thresh.get("CAC Goal", 500)),
        },
        "account_avg": summary.get("last_30d", {}),
        "windows": {
            "yesterday": summary.get("yesterday", {}),
            "last_7d": summary.get("last_7d", {}),
            "last_14d": summary.get("last_14d", {}),
            "last_30d": summary.get("last_30d", {}),
        },
        "campaigns": {"last_7d": insights.get("last_7d", [])[:5]}
    }
    prompt = prompt.replace("{{OVERVIEW_DATA}}", json.dumps(payload))
    prompt = prompt.replace("{{SEASONALITY_CONTEXT}}", "No major seasonal event active.")
    try:
        response = claude.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        return {"insights": [], "error": str(e)}


def detect_anomalies(account_name, summary, thresholds):
    alerts = []
    thresh = thresholds.get(account_name, {})
    l7d = summary.get("last_7d", {})
    l30d = summary.get("last_30d", {})
    roas_min = float(thresh.get("ROAS Min", 1.5))
    cpm_max_pct = float(thresh.get("CPM Max % Increase", 25)) / 100
    cpc_max_pct = float(thresh.get("CPC Max % Increase", 20)) / 100
    ctr_drop_pct = float(thresh.get("CTR Drop % Alert", 30)) / 100

    if l7d.get("roas", 0) < roas_min and l7d.get("spend", 0) > 0:
        alerts.append({
            "type": "roas_low", "severity": "high",
            "metric": "ROAS",
            "message": f"ROAS {l7d['roas']}x below minimum {roas_min}x"
        })
    if l30d.get("cpm", 0) > 0 and l7d.get("cpm", 0) > 0:
        change = (l7d["cpm"] - l30d["cpm"]) / l30d["cpm"]
        if change > cpm_max_pct:
            alerts.append({
                "type": "cpm_spike", "severity": "medium",
                "metric": "CPM",
                "message": f"CPM up {round(change*100)}% vs 30D avg (₹{l7d['cpm']} vs ₹{l30d['cpm']})"
            })
    if l30d.get("cpc", 0) > 0 and l7d.get("cpc", 0) > 0:
        change = (l7d["cpc"] - l30d["cpc"]) / l30d["cpc"]
        if change > cpc_max_pct:
            alerts.append({
                "type": "cpc_spike", "severity": "medium",
                "metric": "CPC",
                "message": f"CPC up {round(change*100)}% vs 30D avg (₹{l7d['cpc']} vs ₹{l30d['cpc']})"
            })
    if l30d.get("ctr", 0) > 0 and l7d.get("ctr", 0) > 0:
        change = (l30d["ctr"] - l7d["ctr"]) / l30d["ctr"]
        if change > ctr_drop_pct:
            alerts.append({
                "type": "ctr_drop", "severity": "medium",
                "metric": "CTR",
                "message": f"CTR dropped {round(change*100)}% vs 30D avg ({l7d['ctr']}% vs {l30d['ctr']}%)"
            })
    return alerts


def get_trello_list_id(list_name="To Do"):
    r = requests.get(
        f"https://api.trello.com/1/boards/{TRELLO_BOARD_ID}/lists",
        params={"key": TRELLO_KEY, "token": TRELLO_TOKEN_VAL}
    )
    r.raise_for_status()
    for lst in r.json():
        if lst["name"] == list_name:
            return lst["id"]
    return None


def create_trello_card(list_id, title, description):
    requests.post(
        "https://api.trello.com/1/cards",
        params={"key": TRELLO_KEY, "token": TRELLO_TOKEN_VAL},
        json={
            "idList": list_id,
            "name": title,
            "desc": description,
            "due": datetime.now(IST).strftime("%Y-%m-%dT23:59:00.000Z")
        }
    )


def create_tasks_in_trello(all_results, team):
    todo_list_id = get_trello_list_id("To Do")
    if not todo_list_id:
        print("Could not find To Do list in Trello")
        return
    date_str = TODAY.strftime("%d %b %Y")
    for result in all_results:
        account_name = result["account_name"]
        owner_email = result["account"].get("Owner", "")
        owner_name = next((t["Name"] for t in team if t["Email"] == owner_email), owner_email)
        for insight in result.get("claude_insights", {}).get("insights", []):
            if insight.get("type") in ["fix", "scale"]:
                title = f"{account_name} — {insight.get('title', 'Action')} | {date_str}"
                desc = f"**Account:** {account_name}\n**Owner:** {owner_name}\n**Type:** {insight.get('type','').upper()}\n\n{insight.get('text','')}\n\n*Auto-generated {date_str}*"
                create_trello_card(todo_list_id, title, desc)
        for alert in result.get("alerts", []):
            if alert["severity"] == "high":
                title = f"{account_name} — {alert['metric']} Alert | {date_str}"
                desc = f"**Account:** {account_name}\n**Owner:** {owner_name}\n**Alert:** {alert['message']}\n\n*Auto-generated {date_str}*"
                create_trello_card(todo_list_id, title, desc)


def roas_color(roas, goal):
    if roas >= goal:
        return "#1a7a4a"
    elif roas >= goal * 0.8:
        return "#d68910"
    return "#c0392b"


def status_badge(status):
    badges = {
        "on_track": ('<span style="background:#e8f5ef;color:#1a7a4a;padding:3px 10px;'
                     'border-radius:10px;font-size:11px;font-weight:600">On Track</span>'),
        "watch": ('<span style="background:#fef9e7;color:#d68910;padding:3px 10px;'
                  'border-radius:10px;font-size:11px;font-weight:600">Watch</span>'),
        "alert": ('<span style="background:#fdecea;color:#c0392b;padding:3px 10px;'
                  'border-radius:10px;font-size:11px;font-weight:600">Alert</span>'),
        "no_data": ('<span style="background:#f0f0f0;color:#888;padding:3px 10px;'
                    'border-radius:10px;font-size:11px;font-weight:600">No Data</span>'),
    }
    return badges.get(status, badges["no_data"])


def build_html_email(all_results, team, thresholds):
    date_str = TODAY.strftime("%A, %d %B %Y")
    total_spend_y = total_spend_7d = total_rev_7d = total_spend_mtd = total_rev_mtd = 0
    on_track = needs_attention = total_alerts = high_alerts = 0
    account_rows = []
    all_alerts_list = []

    for result in all_results:
        s = result["summary"]
        account = result["account"]
        thresh = thresholds.get(account["Account Name"], {})
        roas_goal = float(thresh.get("ROAS Goal", 2.0))
        owner_name = next((t["Name"] for t in team
                          if t["Email"] == account.get("Owner", "")),
                         account.get("Owner", ""))
        y = s.get("yesterday", {})
        l7 = s.get("last_7d", {})
        mtd = s.get("last_30d", {})

        total_spend_y += y.get("spend", 0)
        total_spend_7d += l7.get("spend", 0)
        total_rev_7d += l7.get("revenue", 0)
        total_spend_mtd += mtd.get("spend", 0)
        total_rev_mtd += mtd.get("revenue", 0)

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
            "spend_y": f"{y.get('spend',0):,.0f}",
            "spend_7d": f"{l7.get('spend',0):,.0f}",
            "roas_7d": roas_7d,
            "roas_goal": roas_goal,
            "cpm_7d": f"{l7.get('cpm',0):,.0f}",
            "cpc_7d": f"{l7.get('cpc',0):,.0f}",
            "ctr_7d": l7.get("ctr", 0),
            "purchases_7d": l7.get("purchases", 0),
            "status": status
        })

        if result.get("alerts"):
            all_alerts_list.append({
                "account": account["Account Name"],
                "owner": owner_name,
                "alerts": result["alerts"]
            })
            total_alerts += len(result["alerts"])
            high_alerts += len([a for a in result["alerts"] if a["severity"] == "high"])

    blended_roas_7d = round(total_rev_7d / total_spend_7d, 2) if total_spend_7d > 0 else 0
    blended_roas_mtd = round(total_rev_mtd / total_spend_mtd, 2) if total_spend_mtd > 0 else 0
    active_clients = len([r for r in all_results if r["summary"].get("yesterday", {}).get("spend", 0) > 0])

    # Build tasks by owner
    owner_tasks = {}
    for result in all_results:
        owner_email = result["account"].get("Owner", "")
        owner_name = next((t["Name"] for t in team if t["Email"] == owner_email), owner_email)
        role = next((t.get("Role", "") for t in team if t["Email"] == owner_email), "")
        if owner_email not in owner_tasks:
            owner_tasks[owner_email] = {"name": owner_name, "role": role, "tasks": []}
        for insight in result.get("claude_insights", {}).get("insights", []):
            if insight.get("type") in ["fix", "scale"]:
                owner_tasks[owner_email]["tasks"].append({
                    "account": result["account_name"],
                    "action": insight.get("text", "")[:150],
                    "deadline": "Today EOD",
                    "type": insight.get("type", "")
                })
        for alert in result.get("alerts", []):
            owner_tasks[owner_email]["tasks"].append({
                "account": result["account_name"],
                "action": alert["message"],
                "deadline": "Immediate" if alert["severity"] == "high" else "Today EOD",
                "type": "alert"
            })

    # Build account rows HTML
    acct_rows_html = ""
    for row in account_rows:
        rc = roas_color(row["roas_7d"], row["roas_goal"])
        acct_rows_html += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #f0f0f0;font-weight:600;font-size:12px">{row['name']}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f0f0f0;font-size:12px;color:#666">{row['owner_name']}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f0f0f0;font-size:12px">₹{row['spend_y']}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f0f0f0;font-size:12px">₹{row['spend_7d']}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f0f0f0;font-size:12px;color:{rc};font-weight:700">{row['roas_7d']}×</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f0f0f0;font-size:12px">₹{row['cpm_7d']}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f0f0f0;font-size:12px">₹{row['cpc_7d']}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f0f0f0;font-size:12px">{row['ctr_7d']}%</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f0f0f0;font-size:12px">{row['purchases_7d']}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f0f0f0">{status_badge(row['status'])}</td>
        </tr>"""

    # Build alerts HTML
    alerts_html = ""
    if all_alerts_list:
        for ag in all_alerts_list:
            for alert in ag["alerts"]:
                bg = "#fdecea" if alert["severity"] == "high" else "#fef9e7"
                bc = "#c0392b" if alert["severity"] == "high" else "#d68910"
                alerts_html += f"""
                <div style="background:{bg};border-left:3px solid {bc};padding:10px 14px;
                            border-radius:0 6px 6px 0;margin-bottom:8px">
                  <div style="font-weight:700;font-size:12px;color:{bc}">{ag['account']} — {alert['metric']} Alert</div>
                  <div style="font-size:11px;color:#555;margin-top:3px">{alert['message']} · Owner: {ag['owner']}</div>
                </div>"""
    else:
        alerts_html = '<p style="color:#888;font-size:12px;font-style:italic">No threshold breaches today. All accounts within normal range.</p>'

    # Build insights HTML
    insights_html = ""
    for result in all_results:
        insights = result.get("claude_insights", {}).get("insights", [])
        if insights:
            insights_html += f'<div style="font-weight:700;font-size:13px;margin:12px 0 6px;color:#333">{result["account_name"]}</div>'
            for ins in insights:
                type_colors = {
                    "scale": ("#e8f5ef", "#1a7a4a", "#1a7a4a"),
                    "fix": ("#fdecea", "#c0392b", "#c0392b"),
                    "watch": ("#fef9e7", "#d68910", "#d68910")
                }
                bg, bc, tc = type_colors.get(ins.get("type", "watch"), ("#f7f7f7", "#888", "#333"))
                insights_html += f"""
                <div style="background:{bg};border-left:3px solid {bc};padding:10px 14px;
                            border-radius:0 6px 6px 0;margin-bottom:6px">
                  <div style="font-weight:700;font-size:11px;color:{tc};text-transform:uppercase;margin-bottom:3px">
                    [{ins.get('type','').upper()}] {ins.get('title','')}
                  </div>
                  <div style="font-size:11px;color:#444">{ins.get('text','')}</div>
                </div>"""

    # Build tasks HTML
    tasks_html = ""
    for owner_email, og in owner_tasks.items():
        if og["tasks"]:
            tasks_html += f"""
            <div style="margin-bottom:16px">
              <div style="font-weight:700;font-size:13px;padding-bottom:6px;
                          border-bottom:1px solid #e0e0e0;margin-bottom:8px">
                {og['name']} <span style="color:#888;font-weight:400;font-size:11px">— {og['role']}</span>
              </div>"""
            for task in og["tasks"]:
                type_colors = {"scale": "#1a7a4a", "fix": "#c0392b", "alert": "#c0392b", "watch": "#d68910"}
                tc = type_colors.get(task["type"], "#888")
                tasks_html += f"""
              <div style="display:flex;gap:10px;padding:7px 0;border-bottom:1px solid #f5f5f5;font-size:12px">
                <div style="font-weight:600;min-width:130px;color:#333">{task['account']}</div>
                <div style="flex:1;color:#444">{task['action']}</div>
                <div style="font-size:10px;color:{tc};white-space:nowrap;font-weight:600">{task['deadline']}</div>
              </div>"""
            tasks_html += "</div>"

    roas_color_exec = roas_color(blended_roas_7d, 2.0)
    alert_color = "#c0392b" if total_alerts > 3 else "#d68910" if total_alerts > 0 else "#1a7a4a"

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:'Helvetica Neue',Arial,sans-serif">
<div style="max-width:800px;margin:0 auto;padding:20px">

  <!-- HEADER -->
  <div style="background:#1a1a1a;border-radius:10px 10px 0 0;padding:24px 28px;
              display:flex;justify-content:space-between;align-items:center">
    <div>
      <div style="color:#fff;font-size:22px;font-weight:700;letter-spacing:-0.5px">Carousel Media</div>
      <div style="color:#999;font-size:12px;margin-top:3px">Daily Performance Report</div>
    </div>
    <div style="text-align:right">
      <div style="color:#fff;font-size:13px">{date_str}</div>
      <div style="color:#666;font-size:11px;margin-top:3px">Generated at 8:00 AM IST</div>
    </div>
  </div>

  <!-- EXECUTIVE SUMMARY -->
  <div style="background:#fff;padding:24px 28px;border-left:1px solid #e8e8e8;border-right:1px solid #e8e8e8">
    <div style="font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
                color:#999;margin-bottom:14px">Executive Summary</div>
    <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px">
      <div style="background:#f7f7f7;border-radius:8px;padding:14px 16px">
        <div style="font-size:10px;color:#888;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">Active Clients</div>
        <div style="font-size:22px;font-weight:700">{active_clients}</div>
        <div style="font-size:10px;color:#aaa;margin-top:3px">of {len(all_results)} total</div>
      </div>
      <div style="background:#f7f7f7;border-radius:8px;padding:14px 16px">
        <div style="font-size:10px;color:#888;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">Spend Yesterday</div>
        <div style="font-size:22px;font-weight:700">₹{total_spend_y:,.0f}</div>
        <div style="font-size:10px;color:#aaa;margin-top:3px">₹{total_spend_7d:,.0f} last 7D</div>
      </div>
      <div style="background:#f7f7f7;border-radius:8px;padding:14px 16px">
        <div style="font-size:10px;color:#888;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">Blended ROAS 7D</div>
        <div style="font-size:22px;font-weight:700;color:{roas_color_exec}">{blended_roas_7d}×</div>
        <div style="font-size:10px;color:#aaa;margin-top:3px">{blended_roas_mtd}× MTD</div>
      </div>
      <div style="background:#f7f7f7;border-radius:8px;padding:14px 16px">
        <div style="font-size:10px;color:#888;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">On Track</div>
        <div style="font-size:22px;font-weight:700;color:#1a7a4a">{on_track}</div>
        <div style="font-size:10px;color:#aaa;margin-top:3px">{needs_attention} need attention</div>
      </div>
      <div style="background:#f7f7f7;border-radius:8px;padding:14px 16px">
        <div style="font-size:10px;color:#888;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">Alerts Today</div>
        <div style="font-size:22px;font-weight:700;color:{alert_color}">{total_alerts}</div>
        <div style="font-size:10px;color:#aaa;margin-top:3px">{high_alerts} high priority</div>
      </div>
    </div>
  </div>

  <!-- ACCOUNT SNAPSHOTS -->
  <div style="background:#fff;padding:24px 28px;border-left:1px solid #e8e8e8;border-right:1px solid #e8e8e8;border-top:1px solid #f0f0f0">
    <div style="font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#999;margin-bottom:14px">
      All Accounts — Yesterday | Last 7D | MTD
    </div>
    <table style="width:100%;border-collapse:collapse">
      <thead>
        <tr style="background:#f7f7f7">
          <th style="padding:8px 12px;text-align:left;font-size:10px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.06em">Account</th>
          <th style="padding:8px 12px;text-align:left;font-size:10px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.06em">Owner</th>
          <th style="padding:8px 12px;text-align:left;font-size:10px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.06em">Spend (Y)</th>
          <th style="padding:8px 12px;text-align:left;font-size:10px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.06em">Spend (7D)</th>
          <th style="padding:8px 12px;text-align:left;font-size:10px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.06em">ROAS (7D)</th>
          <th style="padding:8px 12px;text-align:left;font-size:10px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.06em">CPM</th>
          <th style="padding:8px 12px;text-align:left;font-size:10px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.06em">CPC</th>
          <th style="padding:8px 12px;text-align:left;font-size:10px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.06em">CTR</th>
          <th style="padding:8px 12px;text-align:left;font-size:10px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.06em">Purchases</th>
          <th style="padding:8px 12px;text-align:left;font-size:10px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.06em">Status</th>
        </tr>
      </thead>
      <tbody>{acct_rows_html}</tbody>
    </table>
  </div>

  <!-- ALERTS -->
  <div style="background:#fff;padding:24px 28px;border-left:1px solid #e8e8e8;border-right:1px solid #e8e8e8;border-top:1px solid #f0f0f0">
    <div style="font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#999;margin-bottom:14px">
      Alerts & Anomalies
    </div>
    {alerts_html}
  </div>

  <!-- INSIGHTS -->
  <div style="background:#fff;padding:24px 28px;border-left:1px solid #e8e8e8;border-right:1px solid #e8e8e8;border-top:1px solid #f0f0f0">
    <div style="font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#999;margin-bottom:14px">
      AI-Powered Insights
    </div>
    {insights_html if insights_html else '<p style="color:#888;font-size:12px;font-style:italic">No significant insights flagged today.</p>'}
  </div>

  <!-- TASKS -->
  <div style="background:#fff;padding:24px 28px;border-left:1px solid #e8e8e8;border-right:1px solid #e8e8e8;border-top:1px solid #f0f0f0">
    <div style="font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#999;margin-bottom:14px">
      Today's Tasks by Team Member
    </div>
    {tasks_html if tasks_html else '<p style="color:#888;font-size:12px;font-style:italic">No tasks assigned today.</p>'}
  </div>

  <!-- FOOTER -->
  <div style="background:#f7f7f7;border-radius:0 0 10px 10px;padding:16px 28px;
              border:1px solid #e8e8e8;border-top:none;text-align:center">
    <div style="font-size:10px;color:#aaa">
      Carousel Media Reporting System · {date_str} · Auto-generated at 8:00 AM IST
    </div>
    <div style="font-size:10px;color:#aaa;margin-top:3px">
      Tasks synced to Trello → trello.com/b/{TRELLO_BOARD_ID}
    </div>
  </div>

</div>
</body>
</html>"""
    return html


def get_gmail_service():
    creds = Credentials(
        token=None,
        refresh_token=GMAIL_REFRESH_TOKEN,
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token"
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def send_email(html_content, exec_summary, team):
    service = get_gmail_service()
    recipients = [t["Email"] for t in team if t.get("Email")]
    date_str = TODAY.strftime("%d %b %Y")
    alerts = exec_summary["total_alerts"]
    flag = f"⚠️ {alerts} Alert{'s' if alerts != 1 else ''} — " if alerts > 0 else "✅ All Good — "
    subject = f"Carousel Media Daily Report | {flag}{date_str}"

    msg = MIMEMultipart("alternative")
    msg["From"] = GMAIL_SENDER
    msg["To"] = recipients[0]
    if len(recipients) > 1:
        msg["Cc"] = ", ".join(recipients[1:])
    msg["Subject"] = subject
    msg.attach(MIMEText(html_content, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"✅ Email sent to {', '.join(recipients)}")


def main():
    print(f"🚀 Starting Carousel Media Daily Report — {TODAY}")
    print("📊 Loading config from Google Sheets...")
    accounts, thresholds, prompts, team = load_config()
    print(f"   Loaded {len(accounts)} active accounts, {len(team)} team members")

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
                "summary": summary,
                "alerts": alerts,
                "claude_insights": claude_insights
            })
        except Exception as e:
            print(f"   ⚠️ Error processing {account_name}: {e}")
            all_results.append({
                "account_name": account_name,
                "account": account,
                "summary": {},
                "alerts": [],
                "claude_insights": {"insights": []}
            })

    print("📋 Creating Trello cards...")
    try:
        create_tasks_in_trello(all_results, team)
    except Exception as e:
        print(f"   ⚠️ Trello error: {e}")

    print("📧 Building and sending HTML email...")
    html = build_html_email(all_results, team, thresholds)
    total_alerts = sum(len(r.get("alerts", [])) for r in all_results)
    high_alerts = sum(len([a for a in r.get("alerts", []) if a["severity"] == "high"]) for r in all_results)
    send_email(html, {"total_alerts": total_alerts, "high_alerts": high_alerts}, team)
    print("✅ Done!")


if __name__ == "__main__":
    main()
