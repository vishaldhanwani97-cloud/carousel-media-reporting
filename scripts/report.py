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
from calendar import monthrange
from html import escape
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
    thresholds = {r["Account Name"]: r for r in sh.worksheet("Thresholds").get_all_records()}
    prompts = {r["Prompt Name"]: r["Prompt Text"]
               for r in sh.worksheet("Prompts").get_all_records()
               if str(r.get("Active", "")).upper() == "Y"}
    team = sh.worksheet("Team").get_all_records()
    try:
        dna = {r["Account Name"]: r.get("DNA", "")
               for r in sh.worksheet("Account DNA").get_all_records()
               if r.get("Account Name")}
    except Exception:
        dna = {}
    try:
        digests = {r["Account Name"]: r.get("Digest", "")
                   for r in sh.worksheet("Weekly Digest").get_all_records()
                   if r.get("Account Name")}
    except Exception:
        digests = {}
    try:
        open_actions = [r for r in sh.worksheet("Action Log").get_all_records()
                        if str(r.get("Status", "")).lower() == "open"]
    except Exception:
        open_actions = []
    return accounts, thresholds, prompts, team, dna, digests, open_actions


def meta_get(endpoint, params={}):
    params["access_token"] = META_TOKEN
    r = requests.get(f"{META_BASE}/{endpoint}", params=params)
    r.raise_for_status()
    return r.json()


def get_account_insights_range(account_id, since, until):
    fields = (
        "campaign_name,campaign_id,"
        "spend,impressions,clicks,ctr,cpc,cpm,reach,"
        "actions,action_values,frequency"
    )
    data = meta_get(f"act_{account_id}/insights", {
        "level": "campaign",
        "time_range": json.dumps({"since": since, "until": until}),
        "fields": fields,
        "limit": 100
    })
    return data.get("data", [])


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
    from datetime import timedelta
    day_before = (TODAY - timedelta(days=2)).strftime("%Y-%m-%d")
    two_days_ago = (TODAY - timedelta(days=3)).strftime("%Y-%m-%d")
    return {
        "yesterday": get_account_insights(account_id, "yesterday"),
        "day_before": get_account_insights_range(account_id, day_before, day_before),
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
    context_block = ""
    if dna:
        context_block += f"ACCOUNT DNA (permanent knowledge):\n{dna}\n\n"
    if digest:
        context_block += f"LAST WEEK DIGEST:\n{digest}\n\n"
    if account_actions:
        actions_text = "\n".join([
            f"- {a.get('Action Taken', '')} (logged {a.get('Date', '')}, "
            f"expected resolution {a.get('Expected Resolution Date', '')})"
            for a in account_actions
        ])
        context_block += f"OPEN ACTIONS IN FLIGHT (do not re-flag):\n{actions_text}\n\n"
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
            "type": "roas_low", "severity": "high", "metric": "ROAS",
            "message": f"ROAS {l7d['roas']}x below minimum {roas_min}x"
        })
    if l30d.get("cpm", 0) > 0 and l7d.get("cpm", 0) > 0:
        change = (l7d["cpm"] - l30d["cpm"]) / l30d["cpm"]
        if change > cpm_max_pct:
            alerts.append({
                "type": "cpm_spike", "severity": "medium", "metric": "CPM",
                "message": f"CPM up {round(change*100)}% vs 30D avg (Rs{l7d['cpm']} vs Rs{l30d['cpm']})"
            })
    if l30d.get("cpc", 0) > 0 and l7d.get("cpc", 0) > 0:
        change = (l7d["cpc"] - l30d["cpc"]) / l30d["cpc"]
        if change > cpc_max_pct:
            alerts.append({
                "type": "cpc_spike", "severity": "medium", "metric": "CPC",
                "message": f"CPC up {round(change*100)}% vs 30D avg (Rs{l7d['cpc']} vs Rs{l30d['cpc']})"
            })
    if l30d.get("ctr", 0) > 0 and l7d.get("ctr", 0) > 0:
        change = (l30d["ctr"] - l7d["ctr"]) / l30d["ctr"]
        if change > ctr_drop_pct:
            alerts.append({
                "type": "ctr_drop", "severity": "medium", "metric": "CTR",
                "message": f"CTR dropped {round(change*100)}% vs 30D avg ({l7d['ctr']}% vs {l30d['ctr']}%)"
            })
    return alerts


def calculate_pacing(account_name, summary, thresholds):
    thresh = thresholds.get(account_name, {})
    monthly_budget = float(thresh.get("Monthly Budget Goal", 0))
    monthly_revenue = float(thresh.get("Monthly Revenue Goal", 0))
    if monthly_budget == 0 and monthly_revenue == 0:
        return None
    days_in_month = monthrange(TODAY.year, TODAY.month)[1]
    days_elapsed = TODAY.day
    pct_month_elapsed = days_elapsed / days_in_month
    mtd_spend = summary.get("last_30d", {}).get("spend", 0)
    mtd_revenue = summary.get("last_30d", {}).get("revenue", 0)
    budget_pacing = (mtd_spend / monthly_budget * 100) if monthly_budget > 0 else 0
    revenue_pacing = (mtd_revenue / monthly_revenue * 100) if monthly_revenue > 0 else 0
    expected_pct = pct_month_elapsed * 100

    def pacing_status(actual, expected):
        if actual >= expected * 1.15:
            return "overpacing", "#d68910"
        elif actual >= expected * 0.85:
            return "on_pace", "#1a7a4a"
        else:
            return "underpacing", "#c0392b"

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
        return
    date_str = TODAY.strftime("%d %b %Y")
    for result in all_results:
        account_name = result["account_name"]
        owner_email = result["account"].get("Owner", "")
        owner_name = next((t["Name"] for t in team if t["Email"] == owner_email), owner_email)
        for insight in result.get("claude_insights", {}).get("insights", []):
            if insight.get("type") in ["fix", "scale"]:
                title = f"{account_name} - {insight.get('title', 'Action')} | {date_str}"
                desc = f"Account: {account_name}\nOwner: {owner_name}\nType: {insight.get('type','').upper()}\n\n{insight.get('text','')}\n\nAuto-generated {date_str}"
                create_trello_card(todo_list_id, title, desc)
        for alert in result.get("alerts", []):
            if alert["severity"] == "high":
                title = f"{account_name} - {alert['metric']} Alert | {date_str}"
                desc = f"Account: {account_name}\nOwner: {owner_name}\nAlert: {alert['message']}\n\nAuto-generated {date_str}"
                create_trello_card(todo_list_id, title, desc)


def generate_quirky_greeting(all_results, thresholds):
    on_track = sum(1 for r in all_results
                   if r["summary"].get("last_7d", {}).get("roas", 0) >=
                   float(thresholds.get(r["account_name"], {}).get("ROAS Goal", 2.0)))
    alerts = sum(len(r.get("alerts", [])) for r in all_results)
    scale_opps = sum(1 for r in all_results
                     if r["summary"].get("last_7d", {}).get("roas", 0) >=
                     float(thresholds.get(r["account_name"], {}).get("ROAS Goal", 2.0)) * 1.2)
    total = len(all_results)
    prompt = (
        f"You are the AI brain behind Carousel Media's daily performance report. "
        f"Write ONE punchy witty morning greeting line (max 12 words) for the team. "
        f"Reference today's data cleverly. "
        f"Today: {total} accounts, {on_track} on track, {alerts} alerts, {scale_opps} scale opportunities. "
        f"Then write ONE subtitle line (max 10 words) with key stats. "
        f'Output ONLY valid JSON: {{"greeting": "...", "subtitle": "..."}}'
    )
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
        return (data.get("greeting", "Good morning. Let us get to work."),
                data.get("subtitle", f"{on_track} on track · {alerts} alerts · {scale_opps} ready to scale"))
    except Exception:
        return ("Good morning. The pixels worked hard overnight.",
                f"{on_track} on track · {alerts} alerts · {scale_opps} ready to scale")


def generate_account_dna(account_name, insights, summary, thresholds):
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
            campaign_summary.append(
                f"{c.get('campaign_name', 'Unknown')}: Rs{spend:,.0f} spend, {roas}x ROAS"
            )
    prompt = (
        f"You are analysing a Meta ads account for a performance marketing agency in India. "
        f"Based on 30-day data, write a concise Account DNA of 5-8 bullet points. "
        f"Account: {account_name}, ROAS Goal: {thresh.get('ROAS Goal', 2.0)}x, "
        f"30D: Spend Rs{l30.get('spend',0):,.0f}, ROAS {l30.get('roas',0)}x, "
        f"CPM Rs{l30.get('cpm',0):,.0f}, CTR {l30.get('ctr',0)}%, "
        f"7D: ROAS {l7.get('roas',0)}x. "
        f"Top campaigns: {'; '.join(campaign_summary)}. "
        f"Cover: best performing products/campaigns, CPM/CPC benchmarks, known patterns, trajectory. "
        f"Under 200 words. Plain text bullet points starting with -"
    )
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        return f"DNA generation failed: {e}"


def generate_weekly_digest(account_name, insights, summary, alerts):
    l7 = summary.get("last_7d", {})
    l30 = summary.get("last_30d", {})
    prompt = (
        f"Summarise last 7 days of Meta ads for {account_name}. "
        f"7D: Spend Rs{l7.get('spend',0):,.0f}, ROAS {l7.get('roas',0)}x, "
        f"CPM Rs{l7.get('cpm',0):,.0f} (30D avg Rs{l30.get('cpm',0):,.0f}), "
        f"CTR {l7.get('ctr',0)}% (30D avg {l30.get('ctr',0)}%), "
        f"Purchases {l7.get('purchases',0)}. "
        f"Alerts: {len(alerts)}. "
        f"Write 100-150 word digest: what happened, key signals, what was actioned, what to watch. "
        f"Plain text paragraph, past tense, factual."
    )
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        return f"Digest generation failed: {e}"


def save_to_sheet(account_name, tab_name, col_b, col_c, col_d=None):
    try:
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
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SHEETS_ID)
        ws = sh.worksheet(tab_name)
        records = ws.get_all_records()
        for i, r in enumerate(records):
            if r.get("Account Name") == account_name:
                row_num = i + 2
                ws.update(f"B{row_num}", [[col_b]])
                ws.update(f"C{row_num}", [[col_c]])
                if col_d:
                    ws.update(f"D{row_num}", [[col_d]])
                return
        row = [account_name, col_b, col_c]
        if col_d:
            row.append(col_d)
        ws.append_row(row)
    except Exception as e:
        print(f"   Could not save to {tab_name}: {e}")


def build_html_email(all_results, team, thresholds):
    date_str = TODAY.strftime("%A, %d %B %Y")
    greeting, subtitle = generate_quirky_greeting(all_results, thresholds)

    ORANGE = "#F27C38"
    NAVY = "#08415C"
    TEAL = "#2AB6C9"
    BLACK = "#262626"
    WHITE = "#F3F3F3"
    BORDER = "#E8E8E8"
    EMAIL_W = 600
    INNER_W = 560
    CLIENT_CARD_W = 250
    CLIENT_GAP_W = 12
    CLIENT_INNER_W = 212
    METRIC_W = 100
    METRIC_GAP_W = 12
    TEAM_CARD_W = 250
    TEAM_GAP_W = 12
    TEAM_INNER_W = 212

    def s(v):
        return escape(str(v)) if v is not None else ""

    def money(v):
        try:
            v = float(v or 0)
            if v >= 10000000:
                return "Rs" + f"{v/10000000:.2f}Cr"
            if v >= 100000:
                return "Rs" + f"{v/100000:.1f}L"
            if v >= 1000:
                return "Rs" + f"{v/1000:.1f}K"
            return "Rs" + f"{v:,.0f}"
        except Exception:
            return "Rs0"

    def num(v):
        try:
            return f"{int(float(v or 0)):,}"
        except Exception:
            return "0"

    def pct(v):
        try:
            return f"{float(v):.1f}%"
        except Exception:
            return "0%"

    def rfmt(v):
        try:
            return f"{float(v):.2f}x"
        except Exception:
            return "0.00x"

    def bar_w(v, total):
        try:
            return int(max(0, min(float(v), 100)) / 100 * total)
        except Exception:
            return 0

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

    def metric_box(label, value, sub="", vc=BLACK):
        return (
            '<td width="' + str(METRIC_W) + '" valign="top" style="width:' + str(METRIC_W) + 'px;">'
            '<table role="presentation" width="' + str(METRIC_W) + '" cellpadding="0" cellspacing="0" border="0" '
            'style="width:' + str(METRIC_W) + 'px;border-collapse:separate;background:#FFFFFF;border:1px solid ' + BORDER + ';border-radius:6px;">'
            '<tr><td style="padding:10px;font-family:Arial,Helvetica,sans-serif;">'
            '<div style="font-size:9px;text-transform:uppercase;color:#A5A5A5;font-weight:800;letter-spacing:.7px;">' + s(label) + '</div>'
            '<div style="font-size:18px;color:' + vc + ';font-weight:900;margin-top:5px;">' + s(value) + '</div>'
            '<div style="font-size:10px;color:#777;font-weight:700;margin-top:2px;">' + s(sub) + '</div>'
            '</td></tr></table></td>'
        )

    def pacing_block(label, pp, ep, color):
        BAR = 124
        fill = bar_w(pp, BAR)
        empty = BAR - fill
        return (
            '<td width="' + str(METRIC_W) + '" valign="top" style="width:' + str(METRIC_W) + 'px;font-family:Arial,Helvetica,sans-serif;">'
            '<div style="font-size:10px;color:#777;font-weight:700;margin-bottom:4px;">' + s(label) + '</div>'
            '<table role="presentation" width="' + str(BAR) + '" cellpadding="0" cellspacing="0" border="0" '
            'style="width:' + str(BAR) + 'px;border-collapse:collapse;background:#EFEFEF;">'
            '<tr>'
            '<td width="' + str(fill) + '" height="5" style="width:' + str(fill) + 'px;height:5px;background:' + s(color) + ';font-size:0;">&nbsp;</td>'
            '<td width="' + str(empty) + '" height="5" style="width:' + str(empty) + 'px;height:5px;background:#EFEFEF;font-size:0;">&nbsp;</td>'
            '</tr></table>'
            '<div style="font-size:10px;color:' + s(color) + ';font-weight:900;margin-top:4px;">' + pct(pp) +
            ' <span style="color:#999;font-weight:500;">exp. ' + pct(ep) + '</span></div>'
            '</td>'
        )

    def account_card(acct):
        st = acct.get("status", "no_data")
        sl, sbg, sc = status_map.get(st, status_map["no_data"])
        it = acct.get("insight_type")
        il, ibg, ic = insight_map.get(it, insight_map[None])
        p = acct.get("pacing") or {}
        try:
            rc = "#168A43" if float(acct.get("roas_y", 0)) >= float(acct.get("roas_goal", 0)) else "#C0392B"
        except Exception:
            rc = BLACK

        def trend_tag(t):
            if not t:
                return ""
            arrow = "&#9650;" if t["dir"] == "up" else "&#9660;"
            color = "#168A43" if t["dir"] == "up" else "#C0392B"
            return f'<span style="font-size:9px;color:{color};font-weight:900;">{arrow} {t["pct"]}%</span>'

        roas_sub = "vs " + rfmt(acct.get("roas_goal",0)) + " goal " + trend_tag(acct.get("roas_trend",""))
        rev_sub = "Prev Rs" + str(round(float(acct.get("revenue_db",0))/1000,1)) + "K " + trend_tag(acct.get("revenue_trend",""))
        spend_sub = "CTR " + pct(acct.get("ctr_y",0))
        purch_sub = "CPC Rs" + s(acct.get("cpc_y",0)) + " " + trend_tag(acct.get("purchases_trend",""))
        itxt = acct.get("insight_text") or "No critical action needed today."

        pacing_html = ""
        if p:
            pacing_html = (
                '<tr><td style="padding:11px 14px 13px 14px;background:#FAFAFA;border-top:1px solid #EEE;font-family:Arial,Helvetica,sans-serif;">'
                '<div style="font-size:10px;color:#A1A1A1;font-weight:900;text-transform:uppercase;letter-spacing:.9px;margin-bottom:9px;">'
                'June Pacing &mdash; Day ' + s(p.get("days_elapsed","")) + ' of ' + s(p.get("days_in_month","")) + '</div>'
                '<table role="presentation" width="' + str(CLIENT_INNER_W) + '" cellpadding="0" cellspacing="0" border="0" '
                'style="width:' + str(CLIENT_INNER_W) + 'px;border-collapse:collapse;"><tr>'
                + pacing_block("Budget", p.get("budget_pacing_pct",0), p.get("expected_pct",0), p.get("budget_color", ORANGE))
                + '<td width="' + str(METRIC_GAP_W) + '" style="width:' + str(METRIC_GAP_W) + 'px;font-size:0;">&nbsp;</td>'
                + pacing_block("Revenue", p.get("revenue_pacing_pct",0), p.get("expected_pct",0), p.get("revenue_color", ORANGE))
                + '</tr></table></td></tr>'
            )

        return (
            '<table role="presentation" width="' + str(CLIENT_CARD_W) + '" cellpadding="0" cellspacing="0" border="0" '
            'style="width:' + str(CLIENT_CARD_W) + 'px;border-collapse:separate;background:#FFFFFF;border:1px solid ' + BORDER + ';border-radius:8px;">'
            '<tr><td style="background:' + ORANGE + ';border-radius:8px 8px 0 0;padding:14px;">'
            '<table role="presentation" width="276" cellpadding="0" cellspacing="0" border="0" style="width:276px;border-collapse:collapse;">'
            '<tr>'
            '<td width="195" style="width:195px;">'
            '<div style="font-size:14px;color:#FFF;font-weight:900;font-family:Arial,Helvetica,sans-serif;">' + s(acct.get("account_name")) + '</div>'
            '<div style="font-size:10px;color:#FFE0CF;font-weight:700;font-family:Arial,Helvetica,sans-serif;">' + s(acct.get("owner_name")) + ' &middot; Meta</div>'
            '</td>'
            '<td width="81" align="right" style="width:81px;">'
            '<span style="display:inline-block;background:' + sbg + ';color:' + sc + ';font-family:Arial,Helvetica,sans-serif;font-size:10px;font-weight:900;padding:4px 9px;border-radius:14px;">' + s(sl) + '</span>'
            '</td>'
            '</tr></table></td></tr>'
            '<tr><td style="padding:14px;background:#FFFFFF;">'
            '<table role="presentation" width="' + str(CLIENT_INNER_W) + '" cellpadding="0" cellspacing="0" border="0" style="width:' + str(CLIENT_INNER_W) + 'px;border-collapse:collapse;">'
            '<tr>'
            + metric_box("ROAS (Yesterday)", rfmt(acct.get("roas_y",0)), roas_sub, rc)
            + '<td width="' + str(METRIC_GAP_W) + '" style="width:' + str(METRIC_GAP_W) + 'px;font-size:0;">&nbsp;</td>'
            + metric_box("Revenue (Yesterday)", money(acct.get("revenue_y",0)), rev_sub)
            + '</tr><tr><td colspan="3" height="8" style="height:8px;font-size:0;">&nbsp;</td></tr><tr>'
            + metric_box("Spend (Yesterday)", money(acct.get("spend_y",0)), spend_sub)
            + '<td width="' + str(METRIC_GAP_W) + '" style="width:' + str(METRIC_GAP_W) + 'px;font-size:0;">&nbsp;</td>'
            + metric_box("Purchases (Yesterday)", num(acct.get("purchases_y",0)), purch_sub)
            + '</tr></table></td></tr>'
            + pacing_html
            + '<tr><td style="padding:0;border-radius:0 0 8px 8px;">'
            '<table role="presentation" width="' + str(CLIENT_CARD_W) + '" cellpadding="0" cellspacing="0" border="0" '
            'style="width:' + str(CLIENT_CARD_W) + 'px;border-collapse:collapse;background:' + ibg + ';border-radius:0 0 8px 8px;">'
            '<tr>'
            '<td width="4" style="width:4px;background:' + ic + ';font-size:0;">&nbsp;</td>'
            '<td style="padding:12px 14px;font-family:Arial,Helvetica,sans-serif;">'
            '<div style="font-size:10px;color:' + ic + ';font-weight:900;text-transform:uppercase;letter-spacing:.8px;">' + s(il) + '</div>'
            '<div style="font-size:11px;color:' + BLACK + ';font-weight:500;margin-top:5px;line-height:16px;">' + s(itxt) + '</div>'
            '</td></tr></table></td></tr></table>'
        )

    def account_rows(accts):
        rows = []
        for i in range(0, len(accts), 2):
            left = account_card(accts[i])
            right = account_card(accts[i+1]) if i+1 < len(accts) else ""
            rows.append(
                '<tr>'
                '<td width="' + str(CLIENT_CARD_W) + '" valign="top" style="width:' + str(CLIENT_CARD_W) + 'px;">' + left + '</td>'
                '<td width="' + str(CLIENT_GAP_W) + '" style="width:' + str(CLIENT_GAP_W) + 'px;font-size:0;">&nbsp;</td>'
                '<td width="' + str(CLIENT_CARD_W) + '" valign="top" style="width:' + str(CLIENT_CARD_W) + 'px;">' + right + '</td>'
                '</tr>'
                '<tr><td colspan="3" height="14" style="height:14px;font-size:0;">&nbsp;</td></tr>'
            )
        return "".join(rows)

    def task_row(task):
        return (
            '<tr>'
            '<td width="16" valign="top" style="width:16px;padding:8px 0;">'
            '<table role="presentation" width="7" cellpadding="0" cellspacing="0" border="0" style="width:7px;border-collapse:collapse;">'
            '<tr><td width="7" height="7" style="width:7px;height:7px;background:' + s(task.get("color", ORANGE)) + ';border-radius:7px;font-size:0;">&nbsp;</td></tr>'
            '</table></td>'
            '<td width="74" valign="top" style="width:74px;padding:6px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:' + BLACK + ';font-weight:900;">' + s(task.get("account")) + '</td>'
            '<td width="124" valign="top" style="width:124px;padding:6px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#555;">' + s(task.get("action")) + '</td>'
            '<td width="40" align="right" valign="top" style="width:40px;padding:6px 0;font-family:Arial,Helvetica,sans-serif;font-size:10px;color:' + ORANGE + ';font-weight:900;">' + s(task.get("deadline")) + '</td>'
            '</tr>'
        )

    def team_card(member):
        tasks = member.get("tasks") or []
        if tasks:
            task_html = "".join(task_row(t) for t in tasks)
            body = (
                '<tr><td style="padding:10px 14px 14px 14px;">'
                '<table role="presentation" width="' + str(TEAM_INNER_W) + '" cellpadding="0" cellspacing="0" border="0" style="width:' + str(TEAM_INNER_W) + 'px;border-collapse:collapse;">'
                + task_html + '</table></td></tr>'
            )
        else:
            body = '<tr><td style="padding:18px 14px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#999;font-style:italic;">All good &mdash; no action needed today.</td></tr>'

        return (
            '<table role="presentation" width="' + str(TEAM_CARD_W) + '" cellpadding="0" cellspacing="0" border="0" '
            'style="width:' + str(TEAM_CARD_W) + 'px;border-collapse:separate;background:#FFFFFF;border:1px solid ' + BORDER + ';border-radius:8px;">'
            '<tr><td style="background:' + NAVY + ';border-radius:8px 8px 0 0;padding:13px 14px;">'
            '<table role="presentation" width="' + str(TEAM_INNER_W) + '" cellpadding="0" cellspacing="0" border="0" style="width:' + str(TEAM_INNER_W) + 'px;border-collapse:collapse;">'
            '<tr>'
            '<td width="42" valign="middle" style="width:42px;">'
            '<table role="presentation" width="32" cellpadding="0" cellspacing="0" border="0" style="width:32px;border-collapse:collapse;">'
            '<tr><td width="32" height="32" align="center" valign="middle" '
            'style="width:32px;height:32px;background:' + ORANGE + ';border-radius:32px;font-family:Arial,Helvetica,sans-serif;color:#FFF;font-size:11px;line-height:32px;font-weight:900;">'
            + s(member.get("initials")) + '</td></tr></table></td>'
            '<td valign="middle" style="padding-left:8px;">'
            '<div style="font-size:14px;color:#FFF;font-weight:900;font-family:Arial,Helvetica,sans-serif;">' + s(member.get("name")) + '</div>'
            '<div style="font-size:10px;color:' + TEAL + ';font-weight:700;font-family:Arial,Helvetica,sans-serif;margin-top:2px;">' + s(member.get("role")) + '</div>'
            '</td></tr></table></td></tr>'
            + body + '</table>'
        )

    def team_rows(members):
        rows = []
        for i in range(0, len(members), 2):
            left = team_card(members[i])
            right = team_card(members[i+1]) if i+1 < len(members) else ""
            rows.append(
                '<tr>'
                '<td width="' + str(TEAM_CARD_W) + '" valign="top" style="width:' + str(TEAM_CARD_W) + 'px;">' + left + '</td>'
                '<td width="' + str(TEAM_GAP_W) + '" style="width:' + str(TEAM_GAP_W) + 'px;font-size:0;">&nbsp;</td>'
                '<td width="' + str(TEAM_CARD_W) + '" valign="top" style="width:' + str(TEAM_CARD_W) + 'px;">' + right + '</td>'
                '</tr>'
                '<tr><td colspan="3" height="14" style="height:14px;font-size:0;">&nbsp;</td></tr>'
            )
        return "".join(rows)

    # Build data
    accounts = []
    tasks_by_owner = {}

    for result in all_results:
        s_data = result["summary"]
        account = result["account"]
        account_name = result["account_name"]
        thresh = thresholds.get(account_name, {})
        roas_goal = float(thresh.get("ROAS Goal", 2.0))
        owner_email = account.get("Owner", "")
        owner_name = next((t["Name"] for t in team if t["Email"] == owner_email), owner_email)

        y = s_data.get("yesterday", {})
        db = s_data.get("day_before", {})
        l7 = s_data.get("last_7d", {})
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
            top = next((i for i in insights if i.get("type") in ["fix", "scale", "watch"]), None)
            if top:
                insight_type = top.get("type")
                insight_text = top.get("text", "")[:200]

        def trend(today_val, prev_val):
            try:
                today_val = float(today_val or 0)
                prev_val = float(prev_val or 0)
                if prev_val == 0:
                    return ""
                change = ((today_val - prev_val) / prev_val) * 100
                arrow = "up" if change >= 0 else "down"
                return {"pct": round(abs(change), 1), "dir": arrow}
            except Exception:
                return ""

        accounts.append({
            "account_name": account_name,
            "owner_name": owner_name,
            "status": status,
            "roas_goal": roas_goal,
            # Yesterday as primary
            "roas_y": y.get("roas", 0),
            "revenue_y": y.get("revenue", 0),
            "spend_y": y.get("spend", 0),
            "purchases_y": y.get("purchases", 0),
            "ctr_y": y.get("ctr", 0),
            "cpc_y": y.get("cpc", 0),
            # Day before for comparison
            "roas_db": db.get("roas", 0),
            "revenue_db": db.get("revenue", 0),
            "spend_db": db.get("spend", 0),
            "purchases_db": db.get("purchases", 0),
            # Trends
            "roas_trend": trend(y.get("roas",0), db.get("roas",0)),
            "revenue_trend": trend(y.get("revenue",0), db.get("revenue",0)),
            "spend_trend": trend(y.get("spend",0), db.get("spend",0)),
            "purchases_trend": trend(y.get("purchases",0), db.get("purchases",0)),
            # 7D for context
            "roas_7d": roas_7d,
            "roas_7d_val": l7.get("roas", 0),
            "pacing": pacing,
            "insight_type": insight_type,
            "insight_text": insight_text,
        })

        if owner_email not in tasks_by_owner:
            role = next((t.get("Role", "") for t in team if t["Email"] == owner_email), "")
            initials = "".join([n[0].upper() for n in owner_name.split()[:2]])
            tasks_by_owner[owner_email] = {
                "name": owner_name, "initials": initials,
                "role": role, "tasks": []
            }

        if alerts_list:
            top = alerts_list[0]
            tasks_by_owner[owner_email]["tasks"].append({
                "account": account_name[:12],
                "action": top["message"][:90],
                "deadline": "Now" if top["severity"] == "high" else "EOD",
                "color": "#C0392B" if top["severity"] == "high" else "#D66A16"
            })
        elif insights:
            top_ins = next((i for i in insights if i.get("type") in ["fix", "scale"]), None)
            if top_ins:
                tasks_by_owner[owner_email]["tasks"].append({
                    "account": account_name[:12],
                    "action": top_ins.get("text", "")[:90],
                    "deadline": "EOD",
                    "color": "#C0392B" if top_ins.get("type") == "fix" else "#168A43"
                })

    for t in team:
        if t["Email"] not in tasks_by_owner:
            tasks_by_owner[t["Email"]] = {
                "name": t["Name"],
                "initials": "".join([n[0].upper() for n in t["Name"].split()[:2]]),
                "role": t.get("Role", ""),
                "tasks": []
            }

    team_list = list(tasks_by_owner.values())
    clients_html = account_rows(accounts)
    team_html_str = team_rows(team_list)

    IW = str(INNER_W - 48)

    return (
        '<!doctype html><html><head>'
        '<meta charset="utf-8">'
        '<meta name="x-apple-disable-message-reformatting">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Carousel Media Daily Report</title>'
        '</head>'
        '<body style="margin:0;padding:0;background:' + WHITE + ';font-family:Arial,Helvetica,sans-serif;">'
        '<center style="width:100%;background:' + WHITE + ';">'
        '<table role="presentation" width="' + str(EMAIL_W) + '" cellpadding="0" cellspacing="0" border="0" '
        'style="width:' + str(EMAIL_W) + 'px;border-collapse:collapse;background:' + WHITE + ';">'
        '<tr><td style="padding:14px 20px 0 20px;">'

        '<table role="presentation" width="' + str(INNER_W) + '" cellpadding="0" cellspacing="0" border="0" '
        'style="width:' + str(INNER_W) + 'px;border-collapse:collapse;background:' + NAVY + ';border-radius:10px 10px 0 0;">'
        '<tr>'
        '<td width="360" valign="middle" style="width:360px;padding:24px 28px;font-family:Arial,Helvetica,sans-serif;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">'
        '<tr>'
        '<td width="58" valign="middle" style="width:58px;">'
        '<table role="presentation" width="42" cellpadding="0" cellspacing="0" border="0" style="width:42px;border-collapse:collapse;">'
        '<tr><td width="42" height="42" align="center" valign="middle" '
        'style="width:42px;height:42px;background:' + ORANGE + ';border-radius:8px;font-family:Arial,Helvetica,sans-serif;color:#FFF;font-size:23px;line-height:42px;font-weight:900;">C</td></tr>'
        '</table></td>'
        '<td valign="middle" style="padding-left:10px;">'
        '<div style="font-size:21px;color:#FFF;font-weight:900;letter-spacing:.4px;">CAROUSEL MEDIA</div>'
        '<div style="font-size:11px;color:' + TEAL + ';font-weight:900;letter-spacing:1.1px;text-transform:uppercase;margin-top:3px;">Daily Performance Report</div>'
        '</td></tr></table></td>'
        '<td width="260" align="right" valign="middle" style="width:260px;padding:24px 28px;font-family:Arial,Helvetica,sans-serif;">'
        '<div style="font-size:14px;color:#FFF;font-weight:900;">' + escape(date_str) + '</div>'
        '<div style="font-size:11px;color:' + TEAL + ';font-weight:700;margin-top:6px;">Generated 8:00 AM IST</div>'
        '</td></tr></table>'

        '<table role="presentation" width="' + str(INNER_W) + '" cellpadding="0" cellspacing="0" border="0" '
        'style="width:' + str(INNER_W) + 'px;border-collapse:collapse;background:' + ORANGE + ';">'
        '<tr><td align="center" style="padding:16px 30px 15px 30px;font-family:Arial,Helvetica,sans-serif;">'
        '<div style="font-size:15px;color:#FFF;font-weight:900;">' + escape(greeting) + '</div>'
        '<div style="font-size:12px;color:#FFE2D1;font-weight:800;margin-top:4px;">' + escape(subtitle) + '</div>'
        '</td></tr></table>'

        '<table role="presentation" width="' + str(INNER_W) + '" cellpadding="0" cellspacing="0" border="0" '
        'style="width:' + str(INNER_W) + 'px;border-collapse:collapse;background:#FFFFFF;">'
        '<tr><td style="padding:22px 28px 12px 28px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#999;font-weight:900;text-transform:uppercase;letter-spacing:1.6px;">Client Snapshots</td></tr>'
        '<tr><td style="padding:0 28px 4px 28px;">'
        '<table role="presentation" width="' + IW + '" cellpadding="0" cellspacing="0" border="0" style="width:' + IW + 'px;border-collapse:collapse;">'
        + clients_html +
        '</table></td></tr>'
        '<tr><td style="padding:18px 28px 12px 28px;border-top:1px solid #EEE;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#999;font-weight:900;text-transform:uppercase;letter-spacing:1.6px;">Today\'s War Room</td></tr>'
        '<tr><td style="padding:0 28px 8px 28px;">'
        '<table role="presentation" width="' + IW + '" cellpadding="0" cellspacing="0" border="0" style="width:' + IW + 'px;border-collapse:collapse;">'
        + team_html_str +
        '</table></td></tr></table>'

        '<table role="presentation" width="' + str(INNER_W) + '" cellpadding="0" cellspacing="0" border="0" '
        'style="width:' + str(INNER_W) + 'px;border-collapse:collapse;background:' + NAVY + ';border-radius:0 0 10px 10px;">'
        '<tr>'
        '<td width="310" style="width:310px;padding:15px 28px;font-family:Arial,Helvetica,sans-serif;font-size:10px;color:' + TEAL + ';">carouselmedia.in &middot; Tasks synced to Trello</td>'
        '<td width="310" align="right" style="width:310px;padding:15px 28px;font-family:Arial,Helvetica,sans-serif;font-size:10px;color:#A9C7D3;">Powered by Claude AI</td>'
        '</tr></table>'

        '</td></tr>'
        '<tr><td height="26" style="height:26px;font-size:0;">&nbsp;</td></tr>'
        '</table></center></body></html>'
    )


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
    print(f"Email sent to {', '.join(recipients)}")


def main():
    print(f"Starting Carousel Media Daily Report - {TODAY}")
    print("Loading config from Google Sheets...")
    accounts, thresholds, prompts, team, dna, digests, open_actions = load_config()
    print(f"Loaded {len(accounts)} active accounts, {len(team)} team members")

    all_results = []
    for account in accounts:
        account_id = str(account["Meta Account ID"]).replace("act_", "")
        account_name = account["Account Name"]
        print(f"Pulling data for {account_name}...")
        try:
            insights = get_all_insights(account_id)
            summary = compute_account_summary(insights)
            alerts = detect_anomalies(account_name, summary, thresholds)
            print(f"Analysing {account_name} with Claude...")
            account_dna = dna.get(account_name, "")
            account_digest = digests.get(account_name, "")
            claude_insights = analyse_account(
                account, insights, summary, thresholds, prompts,
                dna=account_dna, digest=account_digest, open_actions=open_actions
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
            print(f"Error processing {account_name}: {e}")
            all_results.append({
                "account_name": account_name,
                "account": account,
                "raw_insights": {},
                "summary": {},
                "alerts": [],
                "claude_insights": {"insights": []},
                "pacing": None
            })

    # Auto-generate DNA if missing
    print("Checking Account DNA...")
    for result in all_results:
        account_name = result["account_name"]
        if not dna.get(account_name) and result["summary"].get("last_30d", {}).get("spend", 0) > 0:
            print(f"Generating DNA for {account_name}...")
            generated_dna = generate_account_dna(
                account_name, result["raw_insights"],
                result["summary"], thresholds
            )
            save_to_sheet(account_name, "Account DNA", generated_dna, TODAY.strftime("%Y-%m-%d"))

    # Sunday digest
    if TODAY.weekday() == 6:
        print("Sunday - generating weekly digests...")
        for result in all_results:
            account_name = result["account_name"]
            if result["summary"].get("last_7d", {}).get("spend", 0) > 0:
                print(f"Generating digest for {account_name}...")
                generated_digest = generate_weekly_digest(
                    account_name, result["raw_insights"],
                    result["summary"], result["alerts"]
                )
                save_to_sheet(account_name, "Weekly Digest", generated_digest,
                              TODAY.strftime("%Y-%m-%d"), "Auto-generated")

    print("Creating Trello cards...")
    try:
        create_tasks_in_trello(all_results, team)
    except Exception as e:
        print(f"Trello error: {e}")

    print("Building and sending HTML email...")
    html = build_html_email(all_results, team, thresholds)
    send_email(html, team)
    print("Done!")


if __name__ == "__main__":
    main()
