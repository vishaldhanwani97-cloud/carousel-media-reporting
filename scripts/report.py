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
    """Create rich Trello cards with labels, checklists, auto-assign and routing."""
    date_str = TODAY.strftime("%d %b %Y")

    # List IDs
    LIST_IDS = {
        "incoming":  "6a27b0107f0513b539185df1",
        "urgent":    "6a27b00f4cd09b8c4280da1b",
    }

    # Label IDs — type
    TYPE_LABELS = {
        "alert":    "6a27b011755fdf4737cd0d96",
        "scale":    "6a27b011b39d569d65cd8836",
        "creative": "6a27b01233b453c71efd22f0",
        "strategy": "6a27b0125eeb6f5ece9ed984",
        "comms":    "6a27b0135717f13905e3eafa",
    }

    # Label IDs — account
    ACCOUNT_LABELS = {
        "Iktara Lifestyle":   "6a27b01352e5d084ea40b8da",
        "Kiko Riko":          "6a27b014da32e84b036cfdfd",
        "The Classy Kitchen": "6a27b014d6070db2626013dc",
        "Adawwrably":         "6a27b18e957ba41e2458c7e9",
        "Tribal Veda":        "6a27b18e5eeb86d3e5ba0e1c",
        "Lonnue":             "6a27b18f677e5af38e3e7f76",
        "Cookie Co.":         "6a27b18fe2362dec373653e9",
        "BeAvake":            "6a27b1901fe25744efaf0e2e",
        "Sashays":            "6a27b190abea8949db148152",
        "Aarjavee":           "6a27b191c89a825e2beb10bd",
    }

    # Routing — who owns what type
    def get_owner_email(insight_type, severity):
        if severity == "high":
            return next((t["Email"] for t in team if t.get("Name") == "Vishal"), None)
        if insight_type in ["fix", "alert"]:
            return next((t["Email"] for t in team if t.get("Name") == "Kathan"), None)
        if insight_type == "scale":
            return next((t["Email"] for t in team if t.get("Name") == "Devanshi"), None)
        if insight_type == "strategy":
            return next((t["Email"] for t in team if t.get("Name") == "Nikhil"), None)
        return None

    def get_trello_member_id(email):
        """Get Trello member ID from email."""
        try:
            r = requests.get(
                f"https://api.trello.com/1/members/{email}",
                params={"key": TRELLO_KEY, "token": TRELLO_TOKEN_VAL}
            )
            if r.status_code == 200:
                return r.json().get("id")
        except Exception:
            pass
        return None

    def create_card(list_id, title, desc, label_ids, due_date=None, member_ids=None):
        data = {
            "key": TRELLO_KEY,
            "token": TRELLO_TOKEN_VAL,
            "idList": list_id,
            "name": title,
            "desc": desc,
        }
        if label_ids:
            data["idLabels"] = ",".join(label_ids)
        if due_date:
            data["due"] = due_date
        if member_ids:
            data["idMembers"] = ",".join(member_ids)
        r = requests.post("https://api.trello.com/1/cards", params=data)
        return r.json() if r.status_code == 200 else None

    def add_checklist(card_id, checklist_name, items):
        r = requests.post(
            "https://api.trello.com/1/checklists",
            params={
                "key": TRELLO_KEY,
                "token": TRELLO_TOKEN_VAL,
                "idCard": card_id,
                "name": checklist_name
            }
        )
        if r.status_code != 200:
            return
        checklist_id = r.json()["id"]
        for item in items:
            requests.post(
                f"https://api.trello.com/1/checklists/{checklist_id}/checkItems",
                params={
                    "key": TRELLO_KEY,
                    "token": TRELLO_TOKEN_VAL,
                    "name": item
                }
            )

    from datetime import timezone
    due_today = datetime.now(IST).replace(hour=23, minute=59).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    due_tomorrow = (datetime.now(IST) + timedelta(days=1)).replace(hour=23, minute=59).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    for result in all_results:
        account_name = result["account_name"]
        s = result["summary"]
        l7 = s.get("last_7d", {})
        y = s.get("yesterday", {})
        alerts_list = result.get("alerts", [])
        insights = result.get("claude_insights", {}).get("insights", [])
        account_label = ACCOUNT_LABELS.get(account_name)

        # Process alerts
        for alert in alerts_list:
            severity = alert["severity"]
            metric = alert.get("metric", "Metric")
            message = alert["message"]
            list_id = LIST_IDS["urgent"] if severity == "high" else LIST_IDS["incoming"]
            due = due_today if severity == "high" else due_tomorrow

            owner_email = get_owner_email("alert", severity)
            member_ids = []
            if owner_email:
                mid = get_trello_member_id(owner_email)
                if mid:
                    member_ids = [mid]

            label_ids = [TYPE_LABELS["alert"]]
            if account_label:
                label_ids.append(account_label)

            title = f"{account_name} — {metric} Alert | {date_str}"

            desc = f"""## 📊 Trigger
{message}

## 🔍 Diagnosis
- ROAS Yesterday: {y.get('roas', 0)}x (7D: {l7.get('roas', 0)}x)
- Spend Yesterday: ₹{y.get('spend', 0):,.0f}
- CPM (7D): ₹{l7.get('cpm', 0):,.0f}
- CTR (7D): {l7.get('ctr', 0)}%

## 🛠 Fix Options
- Pause underperforming ad sets and consolidate budget
- Refresh creative — new hooks/angles needed
- Check if any ad sets exited learning phase recently

## 📈 Expected Outcome
ROAS recovery within 48-72h if creative fatigue is the cause.

## 💬 Client Comms Template
Hi [Client], we noticed [metric] has shifted this week. We're actively monitoring and have a plan ready — will keep you updated.

---
*Auto-generated by Carousel Media Reporting System*"""

            card = create_card(list_id, title, desc, label_ids, due, member_ids)
            if card and card.get("id"):
                add_checklist(card["id"], "Action Steps", [
                    "Review affected campaigns in Ads Manager",
                    "Identify root cause (creative / audience / budget)",
                    "Implement fix",
                    "Monitor for 24h",
                    "Update client if needed"
                ])
                print(f"   Created alert card: {title}")

        # Process scale/fix insights
        for insight in insights:
            itype = insight.get("type")
            if itype not in ["fix", "scale", "strategy"]:
                continue

            owner_email = get_owner_email(itype, "medium")
            member_ids = []
            if owner_email:
                mid = get_trello_member_id(owner_email)
                if mid:
                    member_ids = [mid]

            type_label = TYPE_LABELS.get(itype, TYPE_LABELS["strategy"])
            label_ids = [type_label]
            if account_label:
                label_ids.append(account_label)

            emoji = "🔥" if itype == "fix" else "📈" if itype == "scale" else "🧠"
            title = f"{account_name} — {emoji} {insight.get('title', itype.title())} | {date_str}"

            desc = f"""## 📊 Trigger
{insight.get('text', '')}

## 🔍 Context
- ROAS (7D): {l7.get('roas', 0)}x
- Revenue (7D): ₹{l7.get('revenue', 0):,.0f}
- Spend (7D): ₹{l7.get('spend', 0):,.0f}

## 🛠 Recommended Action
{insight.get('action', 'Review account and take appropriate action.')}

## 📈 Expected Outcome
{insight.get('expected_outcome', 'Improvement in account performance within 3-5 days.')}

## 💬 Client Comms Template
Hi [Client], we've identified an opportunity to [improve/fix] [area]. Here's what we're planning to do...

---
*Auto-generated by Carousel Media Reporting System*"""

            card = create_card(LIST_IDS["incoming"], title, desc, label_ids, due_tomorrow, member_ids)
            if card and card.get("id"):
                checklist_items = ["Review data", "Plan action", "Execute", "Monitor results"]
                if itype == "scale":
                    checklist_items = ["Confirm ROAS stable 3+ days", "Identify winning ad set", "Duplicate with +20-30% budget", "Monitor for 48h"]
                elif itype == "fix":
                    checklist_items = ["Identify root cause", "Pause underperformers", "Launch fix", "Verify improvement in 24h"]
                add_checklist(card["id"], "Action Steps", checklist_items)
                print(f"   Created {itype} card: {title}")


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
    from html import escape
    date_str = TODAY.strftime("%A, %d %B %Y")
    greeting, subtitle = generate_quirky_greeting(all_results, thresholds)

    ORANGE = "#F27C38"
    NAVY = "#08415C"
    TEAL = "#2AB6C9"
    BLACK = "#262626"

    def s(v):
        return escape(str(v)) if v is not None else ""

    def money(v):
        try:
            v = float(v or 0)
            if v >= 10000000: return "Rs" + f"{v/10000000:.1f}Cr"
            if v >= 100000: return "Rs" + f"{v/100000:.1f}L"
            if v >= 1000: return "Rs" + f"{v/1000:.1f}K"
            return "Rs" + f"{v:,.0f}"
        except Exception:
            return "Rs0"

    def num(v):
        try: return f"{int(float(v or 0)):,}"
        except Exception: return "0"

    def pct(v):
        try: return f"{float(v):.1f}%"
        except Exception: return "0%"

    def rfmt(v):
        try: return f"{float(v):.2f}x"
        except Exception: return "0.00x"

    def trend(today_val, prev_val):
        try:
            t = float(today_val or 0)
            p = float(prev_val or 0)
            if p == 0: return ""
            chg = ((t - p) / p) * 100
            arrow = "&#9650;" if chg >= 0 else "&#9660;"
            color = "#168A43" if chg >= 0 else "#C0392B"
            return '<span style="color:' + color + ';font-weight:900;">' + arrow + " " + str(round(abs(chg), 1)) + "%</span>"
        except Exception:
            return ""

    def bar(pct_val, color):
        try:
            w = max(0, min(int(float(pct_val or 0)), 100))
        except Exception:
            w = 0
        return (
            '<div style="background:#EFEFEF;height:5px;border-radius:3px;margin-bottom:3px;">'
            '<div style="width:' + str(w) + '%;height:5px;border-radius:3px;background:' + color + ';"></div>'
            '</div>'
        )

    status_map = {
        "on_track": ("On Track", "#E9F8EF", "#15803D"),
        "watch":    ("Watch",    "#FFF3E8", "#D96B12"),
        "alert":    ("Alert",    "#FDECEC", "#C0392B"),
        "no_data":  ("No Data",  "#EEEEEE", "#777777"),
    }
    insight_map = {
        "fix":   ("FIX · IMMEDIATE",    "#FDECEC", "#C0392B"),
        "scale": ("SCALE OPPORTUNITY",  "#E9F8EF", "#168A43"),
        "watch": ("WATCH",              "#FFF3E8", "#D96B12"),
        None:    ("NOTE",               "#F7F7F7", "#999999"),
    }

    def metric_td(label, value, sub=""):
        return (
            '<td style="padding:4px;">'
            '<div style="background:#FAFAFA;border:1px solid #F0F0F0;border-radius:6px;padding:8px 10px;">'
            '<div style="font-size:8px;text-transform:uppercase;letter-spacing:.7px;color:#A5A5A5;font-weight:800;margin-bottom:4px;">' + s(label) + '</div>'
            '<div style="font-size:15px;font-weight:900;color:' + BLACK + ';margin-bottom:2px;">' + value + '</div>'
            '<div style="font-size:9px;color:#777777;font-weight:700;">' + sub + '</div>'
            '</div></td>'
        )

    def build_card(acct):
        st = acct.get("status", "no_data")
        sl, sbg, sc = status_map.get(st, status_map["no_data"])
        it = acct.get("insight_type")
        il, ibg, ic = insight_map.get(it, insight_map[None])
        p = acct.get("pacing") or {}

        try:
            rc = "#168A43" if float(acct.get("roas_y", 0)) >= float(acct.get("roas_goal", 0)) else "#C0392B"
        except Exception:
            rc = BLACK

        roas_sub = "vs " + rfmt(acct.get("roas_goal", 0)) + " goal " + trend(acct.get("roas_y", 0), acct.get("roas_db", 0))
        rev_sub = "Prev " + money(acct.get("revenue_db", 0)) + " " + trend(acct.get("revenue_y", 0), acct.get("revenue_db", 0))
        spend_sub = "CTR " + pct(acct.get("ctr_y", 0))
        purch_sub = "CPC Rs" + s(round(float(acct.get("cpc_y", 0) or 0), 0)) + " " + trend(acct.get("purchases_y", 0), acct.get("purchases_db", 0))

        roas_metric = (
            '<td style="padding:4px;">'
            '<div style="background:#FAFAFA;border:1px solid #F0F0F0;border-radius:6px;padding:8px 10px;">'
            '<div style="font-size:8px;text-transform:uppercase;letter-spacing:.7px;color:#A5A5A5;font-weight:800;margin-bottom:4px;">ROAS (Yesterday)</div>'
            '<div style="font-size:15px;font-weight:900;color:' + rc + ';margin-bottom:2px;">' + rfmt(acct.get("roas_y", 0)) + '</div>'
            '<div style="font-size:9px;color:#777777;font-weight:700;">' + roas_sub + '</div>'
            '</div></td>'
        )

        if p:
            bp = p.get("budget_pacing_pct", 0)
            rp = p.get("revenue_pacing_pct", 0)
            ep = p.get("expected_pct", 0)
            bc = p.get("budget_color", ORANGE)
            rc2 = p.get("revenue_color", ORANGE)
            pacing_html = (
                '<tr><td style="padding:10px 16px;background:#FAFAFA;border-top:1px solid #F0F0F0;">'
                '<div style="font-size:9px;text-transform:uppercase;letter-spacing:.9px;color:#A1A1A1;font-weight:900;margin-bottom:8px;">'
                'June Pacing &mdash; Day ' + s(p.get("days_elapsed","")) + ' of ' + s(p.get("days_in_month","")) + '</div>'
                '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">'
                '<tr>'
                '<td width="48%" style="padding-right:8px;">'
                '<div style="font-size:9px;color:#777;font-weight:700;margin-bottom:3px;">Budget</div>'
                + bar(bp, bc) +
                '<div style="font-size:9px;font-weight:900;color:' + bc + ';">' + pct(bp) + ' <span style="color:#999;font-weight:500;">exp. ' + pct(ep) + '</span></div>'
                '</td>'
                '<td width="4%"></td>'
                '<td width="48%">'
                '<div style="font-size:9px;color:#777;font-weight:700;margin-bottom:3px;">Revenue</div>'
                + bar(rp, rc2) +
                '<div style="font-size:9px;font-weight:900;color:' + rc2 + ';">' + pct(rp) + ' <span style="color:#999;font-weight:500;">exp. ' + pct(ep) + '</span></div>'
                '</td>'
                '</tr></table>'
                '</td></tr>'
            )
        else:
            pacing_html = ""

        insight_text = acct.get("insight_text") or "No critical action needed today."

        return (
            '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="width:100%;border-collapse:collapse;background:#FFFFFF;border:1px solid #E8E8E8;border-radius:8px;margin-bottom:12px;">'
            '<tr><td style="background:' + ORANGE + ';border-radius:8px 8px 0 0;padding:12px 16px;">'
            '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">'
            '<tr>'
            '<td>'
            '<div style="font-size:14px;font-weight:900;color:#FFFFFF;">' + s(acct.get("account_name","")) + '</div>'
            '<div style="font-size:10px;color:rgba(255,255,255,0.8);font-weight:700;margin-top:2px;">' + s(acct.get("owner_name","")) + ' &middot; Meta</div>'
            '</td>'
            '<td align="right">'
            '<span style="display:inline-block;background:' + sbg + ';color:' + sc + ';font-size:10px;font-weight:900;padding:4px 10px;border-radius:12px;">' + s(sl) + '</span>'
            '</td>'
            '</tr></table>'
            '</td></tr>'
            '<tr><td style="padding:8px;">'
            '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">'
            '<tr>'
            + roas_metric
            + metric_td("Revenue (Yesterday)", money(acct.get("revenue_y", 0)), rev_sub)
            + metric_td("Spend (Yesterday)", money(acct.get("spend_y", 0)), spend_sub)
            + metric_td("Purchases", num(acct.get("purchases_y", 0)), purch_sub)
            + '</tr></table>'
            '</td></tr>'
            + pacing_html
            + '<tr><td style="padding:0;border-top:1px solid #F0F0F0;">'
            '<div style="background:' + ibg + ';border-left:3px solid ' + ic + ';padding:10px 16px;border-radius:0 0 8px 8px;">'
            '<div style="font-size:9px;text-transform:uppercase;letter-spacing:.8px;font-weight:900;color:' + ic + ';margin-bottom:4px;">' + s(il) + '</div>'
            '<div style="font-size:11px;color:' + BLACK + ';line-height:1.5;">' + s(insight_text) + '</div>'
            '</div></td></tr>'
            '</table>'
        )

    def build_team_card(member):
        initials = s(member.get("initials", ""))
        name = s(member.get("name", ""))
        role = s(member.get("role", ""))
        tasks = member.get("tasks") or []

        if tasks:
            task_rows = ""
            for task in tasks:
                task_rows += (
                    '<tr><td style="padding:6px 12px;border-bottom:1px solid #F8F8F8;">'
                    '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">'
                    '<tr>'
                    '<td width="8" valign="top" style="padding-top:4px;">'
                    '<div style="width:6px;height:6px;border-radius:50%;background:' + s(task.get("color", ORANGE)) + ';"></div>'
                    '</td>'
                    '<td style="padding-left:6px;font-size:10px;font-weight:900;color:' + BLACK + ';white-space:nowrap;padding-right:6px;">' + s(task.get("account","")[:12]) + '</td>'
                    '<td style="font-size:10px;color:#555555;line-height:1.4;">' + s(task.get("action","")[:80]) + '</td>'
                    '<td align="right" style="font-size:9px;font-weight:900;color:' + ORANGE + ';white-space:nowrap;padding-left:6px;">' + s(task.get("deadline","")) + '</td>'
                    '</tr></table>'
                    '</td></tr>'
                )
            body = '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">' + task_rows + '</table>'
        else:
            body = '<div style="padding:14px 12px;font-size:10px;color:#999999;font-style:italic;">All good &mdash; no action needed today.</div>'

        return (
            '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="width:100%;border-collapse:collapse;background:#FFFFFF;border:1px solid #E8E8E8;border-radius:8px;overflow:hidden;">'
            '<tr><td style="background:' + NAVY + ';padding:10px 12px;border-radius:8px 8px 0 0;">'
            '<table cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">'
            '<tr>'
            '<td style="width:30px;">'
            '<div style="width:28px;height:28px;border-radius:50%;background:' + ORANGE + ';text-align:center;line-height:28px;font-size:10px;font-weight:900;color:#FFFFFF;">' + initials + '</div>'
            '</td>'
            '<td style="padding-left:8px;">'
            '<div style="font-size:12px;font-weight:900;color:#FFFFFF;">' + name + '</div>'
            '<div style="font-size:9px;color:' + TEAL + ';margin-top:2px;">' + role + '</div>'
            '</td>'
            '</tr></table>'
            '</td></tr>'
            '<tr><td>' + body + '</td></tr>'
            '</table>'
        )

    # Build accounts data
    accounts = []
    tasks_by_owner = {}

    for result in all_results:
        sd = result["summary"]
        account = result["account"]
        account_name = result["account_name"]
        thresh = thresholds.get(account_name, {})
        roas_goal = float(thresh.get("ROAS Goal", 2.0))
        owner_email = account.get("Owner", "")
        owner_name = next((t["Name"] for t in team if t["Email"] == owner_email), owner_email)

        y = sd.get("yesterday", {})
        db = sd.get("day_before", {})
        l7 = sd.get("last_7d", {})
        roas_7d = l7.get("roas", 0)
        roas_y = y.get("roas", 0)

        if l7.get("spend", 0) == 0:
            status = "no_data"
        elif roas_y >= roas_goal:
            status = "on_track"
        elif roas_y >= roas_goal * 0.8:
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
            "roas_goal": roas_goal,
            "roas_y": roas_y,
            "roas_db": db.get("roas", 0),
            "revenue_y": y.get("revenue", 0),
            "revenue_db": db.get("revenue", 0),
            "spend_y": y.get("spend", 0),
            "purchases_y": y.get("purchases", 0),
            "purchases_db": db.get("purchases", 0),
            "ctr_y": y.get("ctr", 0),
            "cpc_y": y.get("cpc", 0),
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
                "action": top["message"][:80],
                "deadline": "Now" if top["severity"] == "high" else "EOD",
                "color": "#C0392B" if top["severity"] == "high" else "#D66A16"
            })
        elif insights:
            top_ins = next((i for i in insights if i.get("type") in ["fix","scale"]), None)
            if top_ins:
                tasks_by_owner[owner_email]["tasks"].append({
                    "account": account_name[:12],
                    "action": top_ins.get("text","")[:80],
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

    cards_html = "".join(build_card(a) for a in accounts)

    team_list = list(tasks_by_owner.values())
    team_cards_html = ""
    for i in range(0, len(team_list), 2):
        left = build_team_card(team_list[i])
        right = build_team_card(team_list[i+1]) if i+1 < len(team_list) else ""
        team_cards_html += (
            '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;margin-bottom:10px;">'
            '<tr>'
            '<td width="49%" valign="top" style="width:49%;padding-right:6px;">' + left + '</td>'
            '<td width="2%" style="width:2%;"></td>'
            '<td width="49%" valign="top" style="width:49%;padding-left:6px;">' + (right if right else "") + '</td>'
            '</tr></table>'
        )

    return (
        '<!doctype html><html><head>'
        '<meta charset="utf-8">'
        '<meta name="x-apple-disable-message-reformatting">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '</head>'
        '<body style="margin:0;padding:0;background:#F0F0F0;font-family:Arial,Helvetica,sans-serif;">'
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#F0F0F0;">'
        '<tr><td align="center" style="padding:16px 12px;">'
        '<table width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;border-collapse:collapse;">'

        '<tr><td style="background:' + NAVY + ';border-radius:8px 8px 0 0;padding:16px 20px;">'
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">'
        '<tr>'
        '<td valign="middle">'
        '<table cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">'
        '<tr>'
        '<td style="width:44px;vertical-align:middle;">'
        '<div style="width:38px;height:38px;background:' + ORANGE + ';border-radius:6px;text-align:center;line-height:38px;font-size:20px;font-weight:900;color:#FFFFFF;">C</div>'
        '</td>'
        '<td style="padding-left:10px;vertical-align:middle;">'
        '<div style="font-size:17px;font-weight:900;color:#FFFFFF;letter-spacing:.3px;">CAROUSEL MEDIA</div>'
        '<div style="font-size:9px;font-weight:900;color:' + TEAL + ';text-transform:uppercase;letter-spacing:1px;margin-top:3px;">Daily Performance Report</div>'
        '</td>'
        '</tr></table>'
        '</td>'
        '<td align="right" valign="middle">'
        '<div style="font-size:13px;font-weight:900;color:#FFFFFF;">' + escape(date_str) + '</div>'
        '<div style="font-size:10px;color:' + TEAL + ';margin-top:4px;">Generated 8:00 AM IST</div>'
        '</td>'
        '</tr></table>'
        '</td></tr>'

        '<tr><td style="background:' + ORANGE + ';padding:14px 20px;text-align:center;">'
        '<div style="font-size:14px;font-weight:900;color:#FFFFFF;">' + escape(greeting) + '</div>'
        '<div style="font-size:11px;color:rgba(255,255,255,0.85);font-weight:700;margin-top:4px;">' + escape(subtitle) + '</div>'
        '</td></tr>'

        '<tr><td style="background:#FFFFFF;padding:16px 20px 8px 20px;">'
        '<div style="font-size:9px;text-transform:uppercase;letter-spacing:1.6px;color:#999999;font-weight:900;margin-bottom:12px;">Client Snapshots</div>'
        + cards_html +
        '</td></tr>'

        '<tr><td style="background:#FFFFFF;padding:16px 20px;border-top:1px solid #EEEEEE;">'
        '<div style="font-size:9px;text-transform:uppercase;letter-spacing:1.6px;color:#999999;font-weight:900;margin-bottom:12px;">Today&#39;s War Room</div>'
        + team_cards_html +
        '</td></tr>'

        '<tr><td style="background:' + NAVY + ';border-radius:0 0 8px 8px;padding:12px 20px;">'
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">'
        '<tr>'
        '<td style="font-size:10px;color:' + TEAL + ';">carouselmedia.in &middot; Tasks synced to Trello</td>'
        '<td align="right" style="font-size:10px;color:rgba(255,255,255,0.5);">Powered by Claude AI</td>'
        '</tr></table>'
        '</td></tr>'

        '</table>'
        '</td></tr></table>'
        '</body></html>'
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
