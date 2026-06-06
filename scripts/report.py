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
            "https://www.googleapis.com/auth/spreadsheets"
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
    
    # Load DNA, Weekly Digest, Action Log
    try:
        dna_records = sh.worksheet("Account DNA").get_all_records()
        dna = {r["Account Name"]: r.get("DNA", "") for r in dna_records if r.get("Account Name")}
    except:
        dna = {}
    
    try:
        digest_records = sh.worksheet("Weekly Digest").get_all_records()
        digests = {r["Account Name"]: r.get("Digest", "") for r in digest_records if r.get("Account Name")}
    except:
        digests = {}
    
    try:
        action_records = sh.worksheet("Action Log").get_all_records()
        open_actions = [r for r in action_records 
                       if str(r.get("Status", "")).lower() == "open"]
    except:
        open_actions = []
    
    return accounts, thresholds, prompts, team, dna, digests, open_actions


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


def analyse_account(account, insights, summary, thresholds, prompts,
                    dna="", digest="", open_actions=None):
    thresh = thresholds.get(account["Account Name"], {})
    base_prompt = prompts.get("overview_insights", "")
    account_name = account["Account Name"]
    
    # Filter open actions for this account
    account_actions = [a for a in (open_actions or []) 
                      if a.get("Account Name") == account_name]
    
    payload = {
        "account_name": account_name,
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
    
    # Build enriched prompt with memory layers
    context_block = ""
    
    if dna:
        context_block += f"""
ACCOUNT DNA (permanent knowledge — always apply this):
{dna}

"""
    
    if digest:
        context_block += f"""
LAST WEEK DIGEST (what happened recently):
{digest}

"""
    
    if account_actions:
        actions_text = "\n".join([
            f"- {a.get('Action Taken','')} (logged {a.get('Date','')}, "
            f"expected resolution {a.get('Expected Resolution Date','')})"
            for a in account_actions
        ])
        context_block += f"""
OPEN ACTIONS IN FLIGHT (do not re-flag these as new issues):
{actions_text}

"""
    
    prompt = context_block + base_prompt
    prompt = prompt.replace("{{OVERVIEW_DATA}}", json.dumps(payload))
    prompt = prompt.replace("{{SEASONALITY_CONTEXT}}", "No major seasonal event active.")
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
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


def generate_quirky_greeting(all_results, thresholds):
    """Generate a fresh quirky morning greeting based on today's data."""
    on_track = sum(1 for r in all_results 
                  if r["summary"].get("last_7d", {}).get("roas", 0) >= 
                  float(thresholds.get(r["account_name"], {}).get("ROAS Goal", 2.0)))
    alerts = sum(len(r.get("alerts", [])) for r in all_results)
    scale_opps = sum(1 for r in all_results 
                    if r["summary"].get("last_7d", {}).get("roas", 0) >= 
                    float(thresholds.get(r["account_name"], {}).get("ROAS Goal", 2.0)) * 1.2)
    total = len(all_results)
    
    prompt = f"""You are the AI brain behind Carousel Media's daily performance report.
Write ONE punchy, witty morning greeting line (max 12 words) for the team.
It should be warm, slightly funny, and reference today's actual performance data.
Then write ONE short subtitle line (max 10 words) with the key stats.

Today's data:
- Total accounts: {total}
- On track: {on_track}
- Alerts: {alerts}
- Scale opportunities: {scale_opps}

Rules:
- First line: witty, warm, no corporate speak. Reference the data cleverly.
- Second line: plain stats, factual, like "{on_track} green, {alerts} need attention, {scale_opps} ready to scale"
- Output ONLY valid JSON: {{"greeting": "...", "subtitle": "..."}}
- No markdown, no explanation"""

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        return data.get("greeting", "Good morning. The pixels worked hard overnight."),                data.get("subtitle", f"{on_track} on track · {alerts} need attention · {scale_opps} ready to scale")
    except Exception as e:
        return "Good morning. The pixels worked hard overnight.",                f"{on_track} on track · {alerts} need attention · {scale_opps} ready to scale"


def calculate_pacing(account_name, summary, thresholds):
    """Calculate monthly budget and revenue pacing."""
    thresh = thresholds.get(account_name, {})
    monthly_budget = float(thresh.get("Monthly Budget Goal", 0))
    monthly_revenue = float(thresh.get("Monthly Revenue Goal", 0))
    
    if monthly_budget == 0 and monthly_revenue == 0:
        return None
    
    # Days elapsed in current month
    from calendar import monthrange
    days_in_month = monthrange(TODAY.year, TODAY.month)[1]
    days_elapsed = TODAY.day
    pct_month_elapsed = days_elapsed / days_in_month
    
    # MTD spend and revenue (using last_30d as proxy, scaled to days)
    mtd_spend = summary.get("last_30d", {}).get("spend", 0)
    mtd_revenue = summary.get("last_30d", {}).get("revenue", 0)
    
    # Pacing calculations
    budget_pacing = (mtd_spend / monthly_budget * 100) if monthly_budget > 0 else 0
    revenue_pacing = (mtd_revenue / monthly_revenue * 100) if monthly_revenue > 0 else 0
    expected_pct = pct_month_elapsed * 100
    
    # Status
    def pacing_status(actual_pct, expected_pct):
        if actual_pct >= expected_pct * 1.15:
            return "overpacing", "#c0392b"
        elif actual_pct >= expected_pct * 0.85:
            return "on_pace", "#1a7a4a"
        else:
            return "underpacing", "#d68910"
    
    budget_status, budget_color = pacing_status(budget_pacing, expected_pct)
    revenue_status, revenue_color = pacing_status(revenue_pacing, expected_pct)
    
    return {
        "monthly_budget": monthly_budget,
        "monthly_revenue": monthly_revenue,
        "mtd_spend": mtd_spend,
        "mtd_revenue": mtd_revenue,
        "budget_pacing_pct": round(budget_pacing, 1),
        "revenue_pacing_pct": round(revenue_pacing, 1),
        "expected_pct": round(expected_pct, 1),
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "budget_status": budget_status,
        "budget_color": budget_color,
        "revenue_status": revenue_status,
        "revenue_color": revenue_color,
    }


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
    
    # Generate quirky greeting
    greeting, subtitle = generate_quirky_greeting(all_results, thresholds)
    
    # Build per-account data
    account_cards_html = ""
    tasks_by_owner = {}
    total_alerts = 0
    high_alerts = 0
    on_track_count = 0
    needs_attention_count = 0
    
    for result in all_results:
        s = result["summary"]
        account = result["account"]
        account_name = result["account_name"]
        thresh = thresholds.get(account_name, {})
        roas_goal = float(thresh.get("ROAS Goal", 2.0))
        owner_email = account.get("Owner", "")
        owner_name = next((t["Name"] for t in team if t["Email"] == owner_email), owner_email)
        
        y = s.get("yesterday", {})
        l7 = s.get("last_7d", {})
        
        roas_7d = l7.get("roas", 0)
        if l7.get("spend", 0) == 0:
            status = "no_data"
        elif roas_7d >= roas_goal:
            status = "on_track"
            on_track_count += 1
        elif roas_7d >= roas_goal * 0.8:
            status = "watch"
            needs_attention_count += 1
        else:
            status = "alert"
            needs_attention_count += 1
        
        badge_map = {
            "on_track": ('<span style="background:#e8f5ef;color:#1a7a4a;font-size:9px;'
                        'font-weight:700;padding:3px 8px;border-radius:20px">On Track</span>'),
            "watch": ('<span style="background:#fff3e0;color:#e65100;font-size:9px;'
                     'font-weight:700;padding:3px 8px;border-radius:20px">Watch</span>'),
            "alert": ('<span style="background:#fdecea;color:#c0392b;font-size:9px;'
                     'font-weight:700;padding:3px 8px;border-radius:20px">Alert</span>'),
            "no_data": ('<span style="background:#f0f0f0;color:#888;font-size:9px;'
                       'font-weight:700;padding:3px 8px;border-radius:20px">No Data</span>'),
        }
        badge = badge_map.get(status, badge_map["no_data"])
        
        roas_color = "#1a7a4a" if roas_7d >= roas_goal else "#d68910" if roas_7d >= roas_goal * 0.8 else "#c0392b"
        
        # Pacing
        pacing = result.get("pacing")
        pacing_html = ""
        if pacing:
            def bar_color(status):
                return "#1a7a4a" if status == "on_pace" else "#d68910" if status == "overpacing" else "#c0392b"
            def bar_label(status):
                return "On Pace" if status == "on_pace" else "Overpacing" if status == "overpacing" else "Behind"
            
            b_color = bar_color(pacing["budget_status"])
            r_color = bar_color(pacing["revenue_status"])
            b_width = min(pacing["budget_pacing_pct"], 100)
            r_width = min(pacing["revenue_pacing_pct"], 100)
            
            pacing_html = f"""
            <div style="padding:10px 14px;border-bottom:1px solid #f0f0f0;background:#fff">
              <div style="font-size:9px;color:#aaa;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px;font-weight:700">
                June Pacing — Day {pacing["days_elapsed"]} of {pacing["days_in_month"]}
              </div>
              <div style="display:flex;gap:10px">
                <div style="flex:1">
                  <div style="font-size:9px;color:#888;margin-bottom:3px">Budget</div>
                  <div style="background:#f0f0f0;border-radius:3px;height:4px;width:100%">
                    <div style="width:{b_width}%;height:4px;border-radius:3px;background:{b_color}"></div>
                  </div>
                  <div style="display:flex;justify-content:space-between;margin-top:3px">
                    <span style="font-size:9px;font-weight:700;color:{b_color}">{pacing["budget_pacing_pct"]}% · {bar_label(pacing["budget_status"])}</span>
                    <span style="font-size:9px;color:#aaa">exp. {pacing["expected_pct"]}%</span>
                  </div>
                </div>
                <div style="flex:1">
                  <div style="font-size:9px;color:#888;margin-bottom:3px">Revenue</div>
                  <div style="background:#f0f0f0;border-radius:3px;height:4px;width:100%">
                    <div style="width:{r_width}%;height:4px;border-radius:3px;background:{r_color}"></div>
                  </div>
                  <div style="display:flex;justify-content:space-between;margin-top:3px">
                    <span style="font-size:9px;font-weight:700;color:{r_color}">{pacing["revenue_pacing_pct"]}% · {bar_label(pacing["revenue_status"])}</span>
                    <span style="font-size:9px;color:#aaa">exp. {pacing["expected_pct"]}%</span>
                  </div>
                </div>
              </div>
            </div>"""
        
        # Insight/Alert block
        insight_html = ""
        insights = result.get("claude_insights", {}).get("insights", [])
        alerts_list = result.get("alerts", [])
        
        total_alerts += len(alerts_list)
        high_alerts += len([a for a in alerts_list if a["severity"] == "high"])
        
        if alerts_list:
            top_alert = alerts_list[0]
            insight_html = f"""
            <div style="padding:10px 14px;background:#fdecea;border-left:3px solid #c0392b">
              <div style="font-size:8px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#c0392b;margin-bottom:2px">
                Fix · {'Immediate' if top_alert['severity'] == 'high' else 'Today EOD'}
              </div>
              <div style="font-size:10px;color:#444;line-height:1.5">{top_alert['message']}</div>
            </div>"""
        elif insights:
            top = insights[0]
            border_color = "#1a7a4a" if top.get("type") == "scale" else "#d68910"
            label_color = "#1a7a4a" if top.get("type") == "scale" else "#d68910"
            label = top.get("type", "watch").upper()
            insight_html = f"""
            <div style="padding:10px 14px;background:#fff;border-left:3px solid {border_color}">
              <div style="font-size:8px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:{label_color};margin-bottom:2px">
                {label}
              </div>
              <div style="font-size:10px;color:#444;line-height:1.5">{top.get('text','')[:120]}</div>
            </div>"""
        
        # Build tasks for this owner
        if owner_email not in tasks_by_owner:
            role = next((t.get("Role", "") for t in team if t["Email"] == owner_email), "")
            tasks_by_owner[owner_email] = {
                "name": owner_name,
                "role": role,
                "email": owner_email,
                "tasks": []
            }
        
        for ins in insights:
            if ins.get("type") in ["fix", "scale"]:
                tasks_by_owner[owner_email]["tasks"].append({
                    "account": account_name,
                    "action": ins.get("text", "")[:100],
                    "deadline": "Today EOD",
                    "color": "#c0392b" if ins.get("type") == "fix" else "#1a7a4a"
                })
        
        for alert in alerts_list:
            tasks_by_owner[owner_email]["tasks"].append({
                "account": account_name,
                "action": alert["message"],
                "deadline": "Now" if alert["severity"] == "high" else "Today EOD",
                "color": "#c0392b" if alert["severity"] == "high" else "#d68910"
            })
        
        # Card HTML
        account_cards_html += f"""
        <div style="border:1px solid #e8e8e8;border-radius:10px;overflow:hidden">
          <div style="background:#F27C38;padding:12px 14px;display:flex;justify-content:space-between;align-items:flex-start">
            <div>
              <div style="color:#fff;font-size:12px;font-weight:700">{account_name}</div>
              <div style="color:rgba(255,255,255,0.85);font-size:9px;margin-top:2px;font-weight:500">{owner_name} · Meta</div>
            </div>
            {badge}
          </div>
          <div style="padding:12px 14px;display:grid;grid-template-columns:1fr 1fr;gap:8px;background:#fafafa;border-bottom:1px solid #f0f0f0">
            <div style="background:#fff;border-radius:6px;padding:8px 10px;border:1px solid #f0f0f0">
              <div style="font-size:8px;color:#aaa;text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px;font-weight:600">ROAS (7D)</div>
              <div style="font-size:14px;font-weight:700;color:{roas_color}">{roas_7d}x</div>
              <div style="font-size:9px;margin-top:1px;font-weight:600;color:#888">vs {roas_goal}x goal</div>
            </div>
            <div style="background:#fff;border-radius:6px;padding:8px 10px;border:1px solid #f0f0f0">
              <div style="font-size:8px;color:#aaa;text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px;font-weight:600">Revenue (7D)</div>
              <div style="font-size:14px;font-weight:700;color:#262626">₹{l7.get('revenue',0):,.0f}</div>
              <div style="font-size:9px;margin-top:1px;font-weight:600;color:#888">Yest ₹{y.get('revenue',0):,.0f}</div>
            </div>
            <div style="background:#fff;border-radius:6px;padding:8px 10px;border:1px solid #f0f0f0">
              <div style="font-size:8px;color:#aaa;text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px;font-weight:600">Spend (7D)</div>
              <div style="font-size:14px;font-weight:700;color:#262626">₹{l7.get('spend',0):,.0f}</div>
              <div style="font-size:9px;margin-top:1px;font-weight:600;color:#888">CTR {l7.get('ctr',0)}%</div>
            </div>
            <div style="background:#fff;border-radius:6px;padding:8px 10px;border:1px solid #f0f0f0">
              <div style="font-size:8px;color:#aaa;text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px;font-weight:600">Purchases</div>
              <div style="font-size:14px;font-weight:700;color:#262626">{l7.get('purchases',0)}</div>
              <div style="font-size:9px;margin-top:1px;font-weight:600;color:#888">CPC ₹{l7.get('cpc',0):,.0f}</div>
            </div>
          </div>
          {pacing_html}
          {insight_html}
        </div>"""
    
    # Build team cards
    # Make sure all team members appear even with no tasks
    for t in team:
        if t["Email"] not in tasks_by_owner:
            tasks_by_owner[t["Email"]] = {
                "name": t["Name"],
                "role": t.get("Role", ""),
                "email": t["Email"],
                "tasks": []
            }
    
    team_cards_html = ""
    for owner_email, og in tasks_by_owner.items():
        initials = "".join([n[0].upper() for n in og["name"].split()[:2]])
        tasks_html = ""
        if og["tasks"]:
            for task in og["tasks"]:
                tasks_html += f"""
                <div style="display:flex;gap:8px;padding:6px 0;border-bottom:1px solid #f8f8f8;align-items:flex-start">
                  <div style="width:6px;height:6px;border-radius:50%;background:{task['color']};margin-top:4px;flex-shrink:0"></div>
                  <div style="font-size:10px;font-weight:700;color:#262626;min-width:70px;flex-shrink:0">{task['account'][:12]}</div>
                  <div style="font-size:10px;color:#555;flex:1;line-height:1.4">{task['action'][:80]}</div>
                  <div style="font-size:9px;font-weight:700;white-space:nowrap;color:#F27C38">{task['deadline']}</div>
                </div>"""
        else:
            tasks_html = '<div style="font-size:10px;color:#aaa;font-style:italic;padding:6px 0">All good — no action needed today.</div>'
        
        team_cards_html += f"""
        <div style="border:1px solid #e8e8e8;border-radius:10px;overflow:hidden">
          <div style="background:#08415C;padding:10px 14px;display:flex;align-items:center;gap:10px">
            <div style="width:32px;height:32px;border-radius:50%;background:#F27C38;display:flex;align-items:center;justify-content:center;color:#fff;font-size:11px;font-weight:700;flex-shrink:0">{initials}</div>
            <div>
              <div style="color:#fff;font-size:12px;font-weight:700">{og['name']}</div>
              <div style="color:#2AB6C9;font-size:9px;margin-top:1px">{og['role']}</div>
            </div>
          </div>
          <div style="padding:10px 14px">{tasks_html}</div>
        </div>"""
    
    total_clients = len(all_results)
    
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;background:#f0f0f0;font-family:'Montserrat',Arial,sans-serif">
<div style="max-width:680px;margin:0 auto;padding:20px">

  <div style="background:#08415C;border-radius:12px 12px 0 0;padding:20px 28px;display:flex;justify-content:space-between;align-items:center">
    <div style="display:flex;align-items:center;gap:10px">
      <div style="background:#F27C38;border-radius:8px;width:36px;height:36px;display:flex;align-items:center;justify-content:center">
        <svg viewBox="0 0 20 20" fill="none" width="20" height="20"><rect x="2" y="2" width="5" height="5" rx="1.5" fill="white"/><rect x="9" y="2" width="5" height="5" rx="2" fill="white"/><rect x="16" y="3" width="2" height="3" rx="1" fill="white"/><rect x="2" y="11" width="16" height="3" rx="1.5" fill="white"/></svg>
      </div>
      <div>
        <div style="color:#fff;font-size:16px;font-weight:700;letter-spacing:.5px">CAROUSEL MEDIA</div>
        <div style="color:#2AB6C9;font-size:10px;font-weight:500;margin-top:1px;letter-spacing:.04em;text-transform:uppercase">Daily Performance Report</div>
      </div>
    </div>
    <div style="text-align:right">
      <div style="color:#fff;font-size:12px;font-weight:500">{date_str}</div>
      <div style="color:#2AB6C9;font-size:10px;margin-top:3px">Generated 8:00 AM IST</div>
    </div>
  </div>

  <div style="background:#F27C38;padding:14px 28px">
    <div style="color:#fff;font-size:12px;font-weight:600;text-align:center">{greeting}</div>
    <div style="color:rgba(255,255,255,0.85);font-size:10px;text-align:center;margin-top:4px">{subtitle}</div>
  </div>

  <div style="background:#fff;padding:20px 28px;border-left:1px solid #e8e8e8;border-right:1px solid #e8e8e8">
    <div style="font-size:9px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#999;margin-bottom:14px">Client Snapshots</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
      {account_cards_html}
    </div>
  </div>

  <div style="background:#fff;padding:20px 28px;border-left:1px solid #e8e8e8;border-right:1px solid #e8e8e8;border-top:1px solid #f0f0f0">
    <div style="font-size:9px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#999;margin-bottom:14px">Today's War Room</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      {team_cards_html}
    </div>
  </div>

  <div style="background:#08415C;border-radius:0 0 12px 12px;padding:14px 28px;display:flex;justify-content:space-between;align-items:center">
    <div style="color:#2AB6C9;font-size:9px">carouselmedia.in · Tasks synced to Trello</div>
    <div style="color:#fff;font-size:9px;opacity:.5">Powered by Claude AI</div>
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


def send_email(html_content, team):
    service = get_gmail_service()
    recipients = [t["Email"] for t in team if t.get("Email")]
    date_str = TODAY.strftime("%d %b %Y")
    subject = f"Carousel Media Daily Report | {date_str}"

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


def generate_account_dna(account_name, insights, summary, thresholds):
    """Generate first-draft DNA for an account based on 30D data."""
    thresh = thresholds.get(account_name, {})
    l30 = summary.get("last_30d", {})
    l7 = summary.get("last_7d", {})
    
    campaigns_30d = insights.get("last_30d", [])
    top_campaigns = sorted(campaigns_30d, 
                          key=lambda x: float(x.get("spend", 0)), 
                          reverse=True)[:5]
    
    campaign_summary = []
    for c in top_campaigns:
        spend = float(c.get("spend", 0))
        if spend > 0:
            revenue = extract_revenue(c.get("action_values", []))
            roas = round(revenue / spend, 2) if spend > 0 else 0
            campaign_summary.append(f"{c.get('campaign_name', 'Unknown')}: ₹{spend:,.0f} spend, {roas}x ROAS")
    
    prompt = f"""You are analysing a Meta ads account for a performance marketing agency in India.
Based on the 30-day performance data below, write a concise Account DNA — permanent institutional knowledge about this account.

Account: {account_name}
ROAS Goal: {thresh.get("ROAS Goal", 2.0)}x
CAC Goal: ₹{thresh.get("CAC Goal", 500)}

30D Summary:
- Spend: ₹{l30.get("spend", 0):,.0f}
- ROAS: {l30.get("roas", 0)}x
- CPM: ₹{l30.get("cpm", 0):,.0f}
- CPC: ₹{l30.get("cpc", 0):,.0f}
- CTR: {l30.get("ctr", 0)}%
- Purchases: {l30.get("purchases", 0)}

7D Summary:
- Spend: ₹{l7.get("spend", 0):,.0f}
- ROAS: {l7.get("roas", 0)}x
- CPM: ₹{l7.get("cpm", 0):,.0f}

Top Campaigns by Spend (30D):
{chr(10).join(campaign_summary)}

Write the Account DNA as 5-8 bullet points covering:
- What product/campaign types perform best on ROAS
- Typical CPM and CPC benchmarks for this account
- Known patterns (audience fatigue speed, best performing windows, etc.)
- Any structural notes about campaign setup
- Current performance trajectory (improving/declining/stable)

Keep it under 200 words. Write in plain text with bullet points starting with -
This will be read by an AI every morning as permanent context, so be specific and factual."""

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        return f"DNA generation failed: {e}"


def generate_weekly_digest(account_name, insights, summary, alerts, claude_insights_history):
    """Generate weekly digest — runs every Sunday."""
    l7 = summary.get("last_7d", {})
    l30 = summary.get("last_30d", {})
    
    prompt = f"""You are summarising the last 7 days of Meta ads performance for {account_name}.

7D Performance:
- Spend: ₹{l7.get("spend", 0):,.0f}
- ROAS: {l7.get("roas", 0)}x
- CPM: ₹{l7.get("cpm", 0):,.0f} (30D avg: ₹{l30.get("cpm", 0):,.0f})
- CPC: ₹{l7.get("cpc", 0):,.0f} (30D avg: ₹{l30.get("cpc", 0):,.0f})
- CTR: {l7.get("ctr", 0)}% (30D avg: {l30.get("ctr", 0)}%)
- Purchases: {l7.get("purchases", 0)}

Alerts flagged this week: {len(alerts)}
{chr(10).join([a.get("message", "") for a in alerts])}

Write a 100-150 word weekly digest covering:
- What happened this week (performance up/down/stable + why)
- Key signals observed (fatigue, scaling opportunity, creative issues)
- What was actioned (if anything visible in the data)
- What to watch next week

Plain text, past tense, factual. No bullet points — write as a paragraph.
This replaces last week's digest and will be read as context next week."""

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        return f"Digest generation failed: {e}"


def save_dna_to_sheets(account_name, dna_text, sheets_id, refresh_token, client_id, client_secret):
    """Save generated DNA back to Google Sheet."""
    try:
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=[
                "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/spreadsheets"
            ]
        )
        creds.refresh(Request())
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheets_id)
        ws = sh.worksheet("Account DNA")
        
        # Find existing row or add new one
        records = ws.get_all_records()
        for i, r in enumerate(records):
            if r.get("Account Name") == account_name:
                row_num = i + 2  # +2 for header and 0-index
                ws.update(f"B{row_num}", [[dna_text]])
                ws.update(f"C{row_num}", [[TODAY.strftime("%Y-%m-%d")]])
                print(f"   Updated DNA for {account_name}")
                return
        
        # Add new row if not found
        ws.append_row([account_name, dna_text, TODAY.strftime("%Y-%m-%d")])
        print(f"   Added DNA for {account_name}")
    except Exception as e:
        print(f"   Could not save DNA: {e}")


def save_digest_to_sheets(account_name, digest_text, sheets_id, refresh_token, client_id, client_secret):
    """Save weekly digest back to Google Sheet."""
    try:
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=[
                "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/spreadsheets"
            ]
        )
        creds.refresh(Request())
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheets_id)
        ws = sh.worksheet("Weekly Digest")
        
        records = ws.get_all_records()
        week_of = TODAY.strftime("%Y-%m-%d")
        
        for i, r in enumerate(records):
            if r.get("Account Name") == account_name:
                row_num = i + 2
                ws.update(f"B{row_num}", [[digest_text]])
                ws.update(f"C{row_num}", [[week_of]])
                ws.update(f"D{row_num}", [["Auto-generated"]])
                print(f"   Updated digest for {account_name}")
                return
        
        ws.append_row([account_name, digest_text, week_of, "Auto-generated"])
        print(f"   Added digest for {account_name}")
    except Exception as e:
        print(f"   Could not save digest: {e}")


def main():
    print(f"🚀 Starting Carousel Media Daily Report — {TODAY}")
    print("📊 Loading config from Google Sheets...")
    accounts, thresholds, prompts, team, dna, digests, open_actions = load_config()
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
            account_dna = dna.get(account_name, "")
            account_digest = digests.get(account_name, "")
            claude_insights = analyse_account(
                account, insights, summary, thresholds, prompts,
                dna=account_dna,
                digest=account_digest,
                open_actions=open_actions
            )
            pacing = calculate_pacing(account_name, summary, thresholds)
            all_results.append({
                "account_name": account_name,
                "account": account,
                "raw_insights": insights,
                "summary": summary,
                "alerts": alerts,
                "claude_insights": claude_insights,
                "pacing": pacing
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

    # Auto-generate DNA for accounts that don't have it yet
    print("🧬 Checking Account DNA...")
    for result in all_results:
        account_name = result["account_name"]
        if not dna.get(account_name) and result["summary"].get("last_30d", {}).get("spend", 0) > 0:
            print(f"   Generating DNA for {account_name}...")
            generated_dna = generate_account_dna(
                account_name, result.get("raw_insights", {}), 
                result["summary"], thresholds
            )
            save_dna_to_sheets(
                account_name, generated_dna, SHEETS_ID,
                GMAIL_REFRESH_TOKEN, GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET
            )

    # Generate weekly digest every Sunday
    if TODAY.weekday() == 6:  # 6 = Sunday
        print("📅 Sunday — generating weekly digests...")
        for result in all_results:
            account_name = result["account_name"]
            if result["summary"].get("last_7d", {}).get("spend", 0) > 0:
                print(f"   Generating digest for {account_name}...")
                generated_digest = generate_weekly_digest(
                    account_name, result.get("raw_insights", {}),
                    result["summary"], result["alerts"], []
                )
                save_digest_to_sheets(
                    account_name, generated_digest, SHEETS_ID,
                    GMAIL_REFRESH_TOKEN, GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET
                )

    print("📋 Creating Trello cards...")
    try:
        create_tasks_in_trello(all_results, team)
    except Exception as e:
        print(f"   ⚠️ Trello error: {e}")

    print("📧 Building and sending HTML email...")
    html = build_html_email(all_results, team, thresholds)
    total_alerts = sum(len(r.get("alerts", [])) for r in all_results)
    high_alerts = sum(len([a for a in r.get("alerts", []) if a["severity"] == "high"]) for r in all_results)
    send_email(html, team)
    print("✅ Done!")


if __name__ == "__main__":
    main()
