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
    from html import escape

    date_str = TODAY.strftime("%A, %d %B %Y")
    greeting, subtitle = generate_quirky_greeting(all_results, thresholds)

    ORANGE = "#F27C38"
    NAVY = "#08415C"
    TEAL = "#2AB6C9"
    BLACK = "#262626"
    WHITE = "#F3F3F3"
    CARD_BG = "#FFFFFF"
    BORDER = "#E8E8E8"
    MUTED = "#8C8C8C"
    SOFT = "#FAFAFA"
    EMAIL_W = 680
    INNER_W = 620
    CLIENT_CARD_W = 304
    CLIENT_GAP_W = 14
    CLIENT_INNER_W = 264
    METRIC_W = 128
    METRIC_GAP_W = 8
    TEAM_CARD_W = 304
    TEAM_GAP_W = 14
    TEAM_INNER_W = 264

    def safe(v):
        return escape(str(v)) if v is not None else ""

    def money(v):
        try:
            v = float(v or 0)
            if v >= 10000000:
                return f"&#8377;{v/10000000:.2f}Cr"
            if v >= 100000:
                return f"&#8377;{v/100000:.1f}L"
            if v >= 1000:
                return f"&#8377;{v/1000:.1f}K"
            return f"&#8377;{v:,.0f}"
        except Exception:
            return "&#8377;0"

    def number(v):
        try:
            return f"{int(float(v or 0)):,}"
        except Exception:
            return "0"

    def pct(v):
        try:
            return f"{float(v):.1f}%"
        except Exception:
            return "0.0%"

    def roas_fmt(v):
        try:
            return f"{float(v):.2f}x"
        except Exception:
            return "0.00x"

    def clamp_pct(v):
        try:
            return max(0, min(float(v), 100))
        except Exception:
            return 0

    def bar_width(v, total_w):
        return int((clamp_pct(v) / 100) * total_w)

    status_map = {
        "on_track": ("On Track", "#E9F8EF", "#15803D"),
        "watch": ("Watch", "#FFF3E8", "#D96B12"),
        "alert": ("Alert", "#FDECEC", "#C0392B"),
        "no_data": ("No Data", "#EEEEEE", "#777777"),
    }

    insight_map = {
        "fix": ("FIX IMMEDIATE", "#FDECEC", "#C0392B"),
        "scale": ("SCALE OPPORTUNITY", "#E9F8EF", "#168A43"),
        "watch": ("WATCH", "#FFF3E8", ORANGE),
        None: ("NOTE", "#F7F7F7", "#999999"),
    }

    def metric_box(label, value, sub="", value_color=BLACK):
        return f"""<td width="{METRIC_W}" valign="top" style="width:{METRIC_W}px;">
            <table role="presentation" width="{METRIC_W}" cellpadding="0" cellspacing="0" border="0" style="width:{METRIC_W}px;border-collapse:separate;background:#FFFFFF;border:1px solid {BORDER};border-radius:6px;">
                <tr><td width="{METRIC_W}" style="width:{METRIC_W}px;padding:10px 10px 9px 10px;font-family:Arial,Helvetica,sans-serif;">
                    <div style="font-size:9px;line-height:12px;letter-spacing:.7px;text-transform:uppercase;color:#A5A5A5;font-weight:800;">{safe(label)}</div>
                    <div style="font-size:18px;line-height:22px;color:{value_color};font-weight:900;margin-top:5px;">{safe(value)}</div>
                    <div style="font-size:10px;line-height:13px;color:#777777;font-weight:700;margin-top:2px;">{safe(sub)}</div>
                </td></tr>
            </table>
        </td>"""

    def pacing_block(label, pacing_pct, expected_pct, color):
        BAR_W = 124
        fill = bar_width(pacing_pct, BAR_W)
        empty = BAR_W - fill
        return f"""<td width="{METRIC_W}" valign="top" style="width:{METRIC_W}px;font-family:Arial,Helvetica,sans-serif;">
            <div style="font-size:10px;line-height:13px;color:#777777;font-weight:700;margin-bottom:4px;">{safe(label)}</div>
            <table role="presentation" width="{BAR_W}" cellpadding="0" cellspacing="0" border="0" style="width:{BAR_W}px;border-collapse:collapse;background:#EFEFEF;">
                <tr>
                    <td width="{fill}" height="5" style="width:{fill}px;height:5px;background:{safe(color)};font-size:0;line-height:0;">&nbsp;</td>
                    <td width="{empty}" height="5" style="width:{empty}px;height:5px;background:#EFEFEF;font-size:0;line-height:0;">&nbsp;</td>
                </tr>
            </table>
            <div style="font-size:10px;line-height:13px;color:{safe(color)};font-weight:900;margin-top:4px;">{pct(pacing_pct)} <span style="color:#999999;font-weight:500;">exp. {pct(expected_pct)}</span></div>
        </td>"""

    def account_card(account):
        status = account.get("status", "no_data")
        status_label, status_bg, status_color = status_map.get(status, status_map["no_data"])
        insight_type = account.get("insight_type")
        insight_label, insight_bg, insight_color = insight_map.get(insight_type, insight_map[None])
        pacing = account.get("pacing") or {}
        try:
            roas_color = "#168A43" if float(account.get("roas_7d", 0)) >= float(account.get("roas_goal", 0)) else "#C0392B"
        except Exception:
            roas_color = BLACK
        insight_text = account.get("insight_text") or "No critical action needed today."

        return f"""<table role="presentation" width="{CLIENT_CARD_W}" cellpadding="0" cellspacing="0" border="0" style="width:{CLIENT_CARD_W}px;border-collapse:separate;background:{CARD_BG};border:1px solid {BORDER};border-radius:8px;">
            <tr><td width="{CLIENT_CARD_W}" style="width:{CLIENT_CARD_W}px;background:{ORANGE};border-radius:8px 8px 0 0;padding:14px 14px 13px 14px;font-family:Arial,Helvetica,sans-serif;">
                <table role="presentation" width="276" cellpadding="0" cellspacing="0" border="0" style="width:276px;border-collapse:collapse;">
                    <tr>
                        <td width="195" valign="middle" style="width:195px;">
                            <div style="font-size:14px;line-height:18px;color:#FFFFFF;font-weight:900;">{safe(account.get("account_name"))}</div>
                            <div style="font-size:10px;line-height:14px;color:#FFE0CF;font-weight:700;">{safe(account.get("owner_name"))} &middot; Meta</div>
                        </td>
                        <td width="81" align="right" valign="middle" style="width:81px;">
                            <span style="display:inline-block;background:{status_bg};color:{status_color};font-family:Arial,Helvetica,sans-serif;font-size:10px;font-weight:900;padding:4px 9px;border-radius:14px;">{safe(status_label)}</span>
                        </td>
                    </tr>
                </table>
            </td></tr>
            <tr><td width="{CLIENT_CARD_W}" style="width:{CLIENT_CARD_W}px;padding:14px 14px 12px 14px;background:#FFFFFF;">
                <table role="presentation" width="{CLIENT_INNER_W}" cellpadding="0" cellspacing="0" border="0" style="width:{CLIENT_INNER_W}px;border-collapse:collapse;">
                    <tr>
                        {metric_box("ROAS (7D)", roas_fmt(account.get("roas_7d",0)), f"vs {roas_fmt(account.get('roas_goal',0))} goal", roas_color)}
                        <td width="{METRIC_GAP_W}" style="width:{METRIC_GAP_W}px;font-size:0;">&nbsp;</td>
                        {metric_box("Revenue (7D)", money(account.get("revenue_7d",0)), f"Yesterday {money(account.get('revenue_yesterday',0))}")}
                    </tr>
                    <tr><td colspan="3" height="8" style="height:8px;font-size:0;">&nbsp;</td></tr>
                    <tr>
                        {metric_box("Spend (7D)", money(account.get("spend_7d",0)), f"CTR {pct(account.get('ctr_7d',0))}")}
                        <td width="{METRIC_GAP_W}" style="width:{METRIC_GAP_W}px;font-size:0;">&nbsp;</td>
                        {metric_box("Purchases", number(account.get("purchases_7d",0)), f"CPC &#8377;{safe(account.get('cpc_7d',0))}")}
                    </tr>
                </table>
            </td></tr>
            <tr><td width="{CLIENT_CARD_W}" style="width:{CLIENT_CARD_W}px;padding:11px 14px 13px 14px;background:#FAFAFA;border-top:1px solid #EEEEEE;font-family:Arial,Helvetica,sans-serif;">
                <div style="font-size:10px;color:#A1A1A1;font-weight:900;text-transform:uppercase;letter-spacing:.9px;margin-bottom:9px;">June Pacing &mdash; Day {safe(pacing.get("days_elapsed",""))} of {safe(pacing.get("days_in_month",""))}</div>
                <table role="presentation" width="{CLIENT_INNER_W}" cellpadding="0" cellspacing="0" border="0" style="width:{CLIENT_INNER_W}px;border-collapse:collapse;">
                    <tr>
                        {pacing_block("Budget", pacing.get("budget_pacing_pct",0), pacing.get("expected_pct",0), pacing.get("budget_color", ORANGE))}
                        <td width="{METRIC_GAP_W}" style="width:{METRIC_GAP_W}px;font-size:0;">&nbsp;</td>
                        {pacing_block("Revenue", pacing.get("revenue_pacing_pct",0), pacing.get("expected_pct",0), pacing.get("revenue_color", ORANGE))}
                    </tr>
                </table>
            </td></tr>
            <tr><td width="{CLIENT_CARD_W}" style="width:{CLIENT_CARD_W}px;padding:0;background:#FFFFFF;border-radius:0 0 8px 8px;">
                <table role="presentation" width="{CLIENT_CARD_W}" cellpadding="0" cellspacing="0" border="0" style="width:{CLIENT_CARD_W}px;border-collapse:collapse;background:{insight_bg};border-radius:0 0 8px 8px;">
                    <tr>
                        <td width="4" style="width:4px;background:{insight_color};font-size:0;">&nbsp;</td>
                        <td style="padding:12px 14px 13px 14px;font-family:Arial,Helvetica,sans-serif;">
                            <div style="font-size:10px;color:{insight_color};font-weight:900;text-transform:uppercase;letter-spacing:.8px;">{safe(insight_label)}</div>
                            <div style="font-size:11px;color:{BLACK};font-weight:500;margin-top:5px;line-height:16px;">{safe(insight_text)}</div>
                        </td>
                    </tr>
                </table>
            </td></tr>
        </table>"""

    def account_rows(accounts_list):
        rows = []
        for i in range(0, len(accounts_list), 2):
            left = account_card(accounts_list[i])
            right = account_card(accounts_list[i+1]) if i+1 < len(accounts_list) else ""
            rows.append(f"""<tr>
                <td width="{CLIENT_CARD_W}" valign="top" style="width:{CLIENT_CARD_W}px;">{left}</td>
                <td width="{CLIENT_GAP_W}" style="width:{CLIENT_GAP_W}px;font-size:0;">&nbsp;</td>
                <td width="{CLIENT_CARD_W}" valign="top" style="width:{CLIENT_CARD_W}px;">{right}</td>
            </tr>
            <tr><td colspan="3" height="14" style="height:14px;font-size:0;">&nbsp;</td></tr>""")
        return "".join(rows)

    def task_row(task):
        return f"""<tr>
            <td width="16" valign="top" style="width:16px;padding:8px 0;">
                <table role="presentation" width="7" cellpadding="0" cellspacing="0" border="0" style="width:7px;border-collapse:collapse;">
                    <tr><td width="7" height="7" style="width:7px;height:7px;background:{safe(task.get("color",ORANGE))};border-radius:7px;font-size:0;">&nbsp;</td></tr>
                </table>
            </td>
            <td width="74" valign="top" style="width:74px;padding:6px 6px 6px 0;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:{BLACK};font-weight:900;">{safe(task.get("account"))}</td>
            <td width="124" valign="top" style="width:124px;padding:6px 6px 6px 0;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#555555;">{safe(task.get("action"))}</td>
            <td width="40" align="right" valign="top" style="width:40px;padding:6px 0;font-family:Arial,Helvetica,sans-serif;font-size:10px;color:{ORANGE};font-weight:900;">{safe(task.get("deadline"))}</td>
        </tr>"""

    def team_card(member):
        tasks = member.get("tasks") or []
        if tasks:
            task_html = "".join(task_row(t) for t in tasks)
            body = f"""<tr><td style="padding:10px 14px 14px 14px;">
                <table role="presentation" width="{TEAM_INNER_W}" cellpadding="0" cellspacing="0" border="0" style="width:{TEAM_INNER_W}px;border-collapse:collapse;">{task_html}</table>
            </td></tr>"""
        else:
            body = f"""<tr><td style="padding:18px 14px 22px 14px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#999999;font-style:italic;">All good &mdash; no action needed today.</td></tr>"""

        return f"""<table role="presentation" width="{TEAM_CARD_W}" cellpadding="0" cellspacing="0" border="0" style="width:{TEAM_CARD_W}px;border-collapse:separate;background:#FFFFFF;border:1px solid {BORDER};border-radius:8px;">
            <tr><td width="{TEAM_CARD_W}" style="width:{TEAM_CARD_W}px;background:{NAVY};border-radius:8px 8px 0 0;padding:13px 14px;font-family:Arial,Helvetica,sans-serif;">
                <table role="presentation" width="{TEAM_INNER_W}" cellpadding="0" cellspacing="0" border="0" style="width:{TEAM_INNER_W}px;border-collapse:collapse;">
                    <tr>
                        <td width="42" valign="middle" style="width:42px;">
                            <table role="presentation" width="32" cellpadding="0" cellspacing="0" border="0" style="width:32px;border-collapse:collapse;">
                                <tr><td width="32" height="32" align="center" valign="middle" style="width:32px;height:32px;background:{ORANGE};border-radius:32px;font-family:Arial,Helvetica,sans-serif;color:#FFFFFF;font-size:11px;line-height:32px;font-weight:900;">{safe(member.get("initials"))}</td></tr>
                            </table>
                        </td>
                        <td width="222" valign="middle" style="width:222px;padding-left:8px;">
                            <div style="font-size:14px;color:#FFFFFF;font-weight:900;">{safe(member.get("name"))}</div>
                            <div style="font-size:10px;color:{TEAL};font-weight:700;margin-top:2px;">{safe(member.get("role"))}</div>
                        </td>
                    </tr>
                </table>
            </td></tr>
            {body}
        </table>"""

    def team_rows(team_list):
        rows = []
        for i in range(0, len(team_list), 2):
            left = team_card(team_list[i])
            right = team_card(team_list[i+1]) if i+1 < len(team_list) else ""
            rows.append(f"""<tr>
                <td width="{TEAM_CARD_W}" valign="top" style="width:{TEAM_CARD_W}px;">{left}</td>
                <td width="{TEAM_GAP_W}" style="width:{TEAM_GAP_W}px;font-size:0;">&nbsp;</td>
                <td width="{TEAM_CARD_W}" valign="top" style="width:{TEAM_CARD_W}px;">{right}</td>
            </tr>
            <tr><td colspan="3" height="14" style="height:14px;font-size:0;">&nbsp;</td></tr>""")
        return "".join(rows)

    # Build accounts and team data
    accounts = []
    tasks_by_owner = {}

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
        elif roas_7d >= roas_goal * 0.8:
            status = "watch"
        else:
            status = "alert"

        pacing = result.get("pacing")
        alerts_list = result.get("alerts", [])
        insights = result.get("claude_insights", {}).get("insights", [])

        insight_type = None
        insight_text = "No critical action needed today."
        if alerts_list:
            insight_type = "fix"
            insight_text = alerts_list[0]["message"]
        elif insights:
            top = next((i for i in insights if i.get("type") in ["fix","scale","watch"]), None)
            if top:
                insight_type = top.get("type")
                insight_text = top.get("text","")[:200]

        accounts.append({
            "account_name": account_name,
            "owner_name": owner_name,
            "status": status,
            "roas_7d": roas_7d,
            "roas_goal": roas_goal,
            "revenue_7d": l7.get("revenue", 0),
            "revenue_yesterday": y.get("revenue", 0),
            "spend_7d": l7.get("spend", 0),
            "ctr_7d": l7.get("ctr", 0),
            "cpc_7d": l7.get("cpc", 0),
            "purchases_7d": l7.get("purchases", 0),
            "purchases_yesterday": y.get("purchases", 0),
            "pacing": pacing,
            "insight_type": insight_type,
            "insight_text": insight_text,
        })

        if owner_email not in tasks_by_owner:
            role = next((t.get("Role","") for t in team if t["Email"] == owner_email), "")
            initials = "".join([n[0].upper() for n in owner_name.split()[:2]])
            tasks_by_owner[owner_email] = {"name": owner_name, "initials": initials, "role": role, "tasks": []}

        if alerts_list:
            top = alerts_list[0]
            tasks_by_owner[owner_email]["tasks"].append({
                "account": account_name[:12],
                "action": top["message"][:90],
                "deadline": "Now" if top["severity"] == "high" else "EOD",
                "color": "#C0392B" if top["severity"] == "high" else "#D66A16"
            })
        elif insights:
            top_ins = next((i for i in insights if i.get("type") in ["fix","scale"]), None)
            if top_ins:
                tasks_by_owner[owner_email]["tasks"].append({
                    "account": account_name[:12],
                    "action": top_ins.get("text","")[:90],
                    "deadline": "EOD",
                    "color": "#C0392B" if top_ins.get("type") == "fix" else "#168A43"
                })

    for t in team:
        if t["Email"] not in tasks_by_owner:
            tasks_by_owner[t["Email"]] = {
                "name": t["Name"],
                "initials": "".join([n[0].upper() for n in t["Name"].split()[:2]]),
                "role": t.get("Role",""),
                "tasks": []
            }

    team_list = list(tasks_by_owner.values())
    clients_html = account_rows(accounts)
    team_html_str = team_rows(team_list)

    return f"""<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="x-apple-disable-message-reformatting">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Carousel Media Daily Report</title>
</head>
<body style="margin:0;padding:0;background:{WHITE};font-family:Arial,Helvetica,sans-serif;">
<center style="width:100%;background:{WHITE};">
<table role="presentation" width="{EMAIL_W}" cellpadding="0" cellspacing="0" border="0" style="width:{EMAIL_W}px;border-collapse:collapse;background:{WHITE};">
<tr><td style="padding:14px 20px 0 20px;">
<table role="presentation" width="{INNER_W}" cellpadding="0" cellspacing="0" border="0" style="width:{INNER_W}px;border-collapse:collapse;background:{NAVY};border-radius:10px 10px 0 0;">
<tr>
    <td width="360" valign="middle" style="width:360px;padding:24px 28px;font-family:Arial,Helvetica,sans-serif;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
            <tr>
                <td width="58" valign="middle" style="width:58px;">
                    <table role="presentation" width="42" cellpadding="0" cellspacing="0" border="0" style="width:42px;border-collapse:collapse;">
                        <tr><td width="42" height="42" align="center" valign="middle" style="width:42px;height:42px;background:{ORANGE};border-radius:8px;font-family:Arial,Helvetica,sans-serif;color:#FFFFFF;font-size:23px;line-height:42px;font-weight:900;">C</td></tr>
                    </table>
                </td>
                <td valign="middle" style="padding-left:10px;">
                    <div style="font-size:21px;line-height:25px;color:#FFFFFF;font-weight:900;letter-spacing:.4px;">CAROUSEL MEDIA</div>
                    <div style="font-size:11px;line-height:15px;color:{TEAL};font-weight:900;letter-spacing:1.1px;text-transform:uppercase;margin-top:3px;">Daily Performance Report</div>
                </td>
            </tr>
        </table>
    </td>
    <td width="260" align="right" valign="middle" style="width:260px;padding:24px 28px;font-family:Arial,Helvetica,sans-serif;">
        <div style="font-size:14px;color:#FFFFFF;font-weight:900;">{date_str}</div>
        <div style="font-size:11px;color:{TEAL};font-weight:700;margin-top:6px;">Generated 8:00 AM IST</div>
    </td>
</tr>
</table>
<table role="presentation" width="{INNER_W}" cellpadding="0" cellspacing="0" border="0" style="width:{INNER_W}px;border-collapse:collapse;background:{ORANGE};">
<tr><td align="center" style="padding:16px 30px 15px 30px;font-family:Arial,Helvetica,sans-serif;">
    <div style="font-size:15px;color:#FFFFFF;font-weight:900;">{greeting}</div>
    <div style="font-size:12px;color:#FFE2D1;font-weight:800;margin-top:4px;">{subtitle}</div>
</td></tr>
</table>
<table role="presentation" width="{INNER_W}" cellpadding="0" cellspacing="0" border="0" style="width:{INNER_W}px;border-collapse:collapse;background:#FFFFFF;">
<tr><td style="padding:22px 28px 12px 28px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#999999;font-weight:900;text-transform:uppercase;letter-spacing:1.6px;">Client Snapshots</td></tr>
<tr><td style="padding:0 28px 4px 28px;">
    <table role="presentation" width="{INNER_W - 56}" cellpadding="0" cellspacing="0" border="0" style="width:{INNER_W - 56}px;border-collapse:collapse;">
        {clients_html}
    </table>
</td></tr>
<tr><td style="padding:18px 28px 12px 28px;border-top:1px solid #EEEEEE;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#999999;font-weight:900;text-transform:uppercase;letter-spacing:1.6px;">Today's War Room</td></tr>
<tr><td style="padding:0 28px 8px 28px;">
    <table role="presentation" width="{INNER_W - 56}" cellpadding="0" cellspacing="0" border="0" style="width:{INNER_W - 56}px;border-collapse:collapse;">
        {team_html_str}
    </table>
</td></tr>
</table>
<table role="presentation" width="{INNER_W}" cellpadding="0" cellspacing="0" border="0" style="width:{INNER_W}px;border-collapse:collapse;background:{NAVY};border-radius:0 0 10px 10px;">
<tr>
    <td width="310" style="width:310px;padding:15px 28px;font-family:Arial,Helvetica,sans-serif;font-size:10px;color:{TEAL};">carouselmedia.in &middot; Tasks synced to Trello</td>
    <td width="310" align="right" style="width:310px;padding:15px 28px;font-family:Arial,Helvetica,sans-serif;font-size:10px;color:#A9C7D3;">Powered by Claude AI</td>
</tr>
</table>
</td></tr>
<tr><td height="26" style="height:26px;font-size:0;">&nbsp;</td></tr>
</table>
</center>
</body>
</html>


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
