# Carousel Media — Daily Reporting System

Automated daily Meta Ads performance report. Runs every morning at 8:00 AM IST via GitHub Actions.

## What it does
- Pulls Meta Ads data for all active accounts (yesterday, L7D, L14D, L30D)
- Detects anomalies against per-client thresholds
- Sends data to Claude API for AI-powered decision recommendations
- Creates Trello cards for flagged actions, assigned to the right team member
- Generates a visual PDF report
- Sends one email to the full team with executive summary + full report attached

## Repo structure
```
.github/workflows/daily_report.yml   # GitHub Actions scheduler (8am IST daily)
scripts/report.py                    # Main Python script
requirements.txt                     # Python dependencies
```

## GitHub Secrets required
| Secret | Description |
|--------|-------------|
| `META_ACCESS_TOKEN` | Meta system user long-lived token |
| `CLAUDE_API_KEY` | Anthropic API key |
| `GMAIL_CLIENT_ID` | Google OAuth client ID |
| `GMAIL_CLIENT_SECRET` | Google OAuth client secret |
| `GMAIL_REFRESH_TOKEN` | Gmail OAuth refresh token |
| `GMAIL_SENDER` | Sender email (vishal@carouselmedia.in) |
| `TRELLO_API_KEY` | Trello API key |
| `TRELLO_TOKEN` | Trello token |
| `TRELLO_BOARD_ID` | Trello board ID (xccqsFqe) |
| `GOOGLE_SHEETS_ID` | Google Sheet ID for config |
| `GOOGLE_SERVICE_ACCOUNT` | Google service account JSON (for Sheets access) |

## Google Sheet structure
The script reads config from a Google Sheet with 5 tabs:

### Tab 1: Accounts
| Column | Description |
|--------|-------------|
| Account Name | Client name |
| Meta Account ID | Numeric ID (without act_) |
| Owner | Owner email address |
| Active | Y or N |
| Platform | Meta / Google / Both |

### Tab 2: Thresholds
| Column | Description |
|--------|-------------|
| Account Name | Must match Tab 1 exactly |
| ROAS Goal | Target ROAS (e.g. 2.5) |
| ROAS Min | Minimum acceptable ROAS before alert |
| CAC Goal | Target cost per purchase |
| Max Frequency | Frequency threshold |
| CPM Max % Increase | % increase vs 30D avg to trigger alert |
| CPC Max % Increase | % increase vs 30D avg to trigger alert |
| CTR Drop % Alert | % drop vs 30D avg to trigger alert |
| Min Purchases | Minimum purchases before Claude makes a decision |

### Tab 3: Prompts
| Column | Description |
|--------|-------------|
| Prompt Name | overview_insights / campaign_analysis / adset_analysis |
| Prompt Text | Full prompt text |
| Active | Y or N |

### Tab 4: Team
| Column | Description |
|--------|-------------|
| Name | Full name |
| Email | Work email |
| Role | War Council / Campaign Commander |
| Trello Username | @username |

### Tab 5: Report Log
Auto-populated by the script. Do not edit manually.
| Date | Accounts Processed | Alerts Fired | Email Sent | Status |

## Manual trigger
Go to GitHub → Actions → Daily Meta Ads Report → Run workflow
Useful for testing without waiting for 8am.

## Adding a new client
1. Open the Google Sheet
2. Add a row in Tab 1 (Accounts) with Active = Y
3. Add a row in Tab 2 (Thresholds) with the client's specific goals
4. The script picks it up automatically next morning

## Removing a client
Set Active = N in Tab 1. No code changes needed.
