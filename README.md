# 🔍 Argus — Competitive Intelligence Monitor

> **Your ops and GTM team are spending hours every week manually checking competitor pricing pages, product blogs, and industry news feeds.** Argus does it overnight: scrape a configurable URL list, detect what changed, summarize it with an LLM, and deliver a formatted email digest — every morning before standup.

---

## The Problem

Staying current on competitors is critical but brutally manual. Someone has to remember to check the pricing page after the industry conference. Someone has to notice the competitor's blog post about a new enterprise tier. Someone has to read through three newsletters to find the one paragraph that matters.

**Argus automates the monitoring loop so your team can focus on responding, not reading.**

---

## How It Works

```
config.yaml                          .env
(URL list, schedule,     +      (API keys,
 email settings)               SMTP credentials)
       │
       ▼
┌─────────────────────────────────────────────────────┐
│                   run_digest()                       │
│                                                     │
│  For each URL:                                      │
│  ┌─────────────┐   ┌──────────────┐   ┌──────────┐ │
│  │  scraper.py │──▶│    diff.py   │──▶│ SQLite   │ │
│  │  (requests  │   │ (SHA-256 of  │   │ snapshot │ │
│  │  + BS4)     │   │  body text)  │   │ store    │ │
│  └─────────────┘   └──────────────┘   └──────────┘ │
│          │ changed?                                  │
│          ▼                                          │
│  ┌────────────────┐                                 │
│  │ summarizer.py  │ claude-sonnet-4-20250514        │
│  │ (Anthropic SDK │ with prompt caching             │
│  │  + caching)    │                                 │
│  └────────────────┘                                 │
│          │                                          │
│          ▼                                          │
│  ┌────────────────┐                                 │
│  │  emailer.py    │ HTML digest → SMTP / SendGrid   │
│  └────────────────┘                                 │
└─────────────────────────────────────────────────────┘
       │
       ▼
  APScheduler (cron) — runs daily at 6 AM UTC
```

**Change detection** uses a content-aware hash: navigation, headers, footers, scripts, and other boilerplate are stripped before hashing. This means a new blog post or a pricing change triggers the alert — but a nav link counter ticking up does not.

**Summarization** uses prompt caching on the system prompt. The LLM is called once per changed source, and because the system instructions are identical across calls in a single run, the API serves them from cache after the first call (~10% of the normal token cost).

---

## Project Structure

```
argus/
├── argus/
│   ├── scraper.py      # HTTP fetch + BeautifulSoup content extraction
│   ├── diff.py         # SHA-256 hash-based change detection (pure, no I/O)
│   ├── summarizer.py   # Anthropic API calls with prompt caching
│   ├── emailer.py      # HTML digest builder + SMTP / SendGrid delivery
│   ├── storage.py      # SQLite schema + CRUD (page snapshots + run log)
│   └── scheduler.py    # APScheduler wiring + run_digest() orchestration
├── main.py             # CLI: `run` or `schedule`
├── config.yaml         # URL list, email settings, cron schedule
├── .env.example        # API keys template
└── requirements.txt
```

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/emiliomt/argus.git
cd argus
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure your environment

```bash
cp .env.example .env
```

Edit `.env` and fill in:
- `ANTHROPIC_API_KEY` — from [console.anthropic.com](https://console.anthropic.com/keys)
- `SMTP_USER` + `SMTP_PASSWORD` — your sending email + app password

> **Gmail users:** You need an [App Password](https://myaccount.google.com/apppasswords), not your regular account password. 2-Step Verification must be enabled.

### 3. Configure your sources and email

Edit `config.yaml`:

```yaml
sources:
  - name: "Competitor X — Pricing"
    url: "https://competitorx.com/pricing"
  - name: "Industry Blog"
    url: "https://relevantblog.com/"

email:
  from: "argus@yourcompany.com"
  to:
    - "you@yourcompany.com"
  provider: "smtp"

smtp:
  host: "smtp.gmail.com"
  port: 587
  use_tls: true
```

### 4. Run it

**One-shot run** (great for testing):
```bash
python main.py run
```

**Start the scheduler** (runs on the cron schedule in config.yaml):
```bash
python main.py schedule
```

**Use a different config file:**
```bash
python main.py run --config /etc/argus/config.yaml
```

---

## Example Output

**Console log (one-shot run):**
```
2026-05-25T06:00:01Z  INFO      argus.scheduler  Starting Argus digest run
2026-05-25T06:00:01Z  INFO      argus.scheduler  Checking: Competitor X — Pricing (https://competitorx.com/pricing)
2026-05-25T06:00:02Z  INFO      argus.diff       Content change detected: https://competitorx.com/pricing
2026-05-25T06:00:02Z  INFO      argus.scheduler  Checking: Industry Blog (https://relevantblog.com/)
2026-05-25T06:00:03Z  INFO      argus.diff       No change: https://relevantblog.com/
2026-05-25T06:00:03Z  INFO      argus.scheduler  Summarising changes for: Competitor X — Pricing
2026-05-25T06:00:06Z  INFO      argus.summarizer Summarised Competitor X — Pricing — tokens: input=1842 cached=312 output=287
2026-05-25T06:00:06Z  INFO      argus.emailer    Digest delivered successfully to 1 recipients
2026-05-25T06:00:06Z  INFO      argus.storage    Run logged: checked=2 changed=1 email_sent=True
2026-05-25T06:00:06Z  INFO      argus.scheduler  Argus digest run complete.
```

**Email digest excerpt:**

---
> **Subject:** [Argus] Competitive Intel Digest — May 25, 2026
>
> **Competitor X — Pricing** `CHANGED`
> https://competitorx.com/pricing
>
> Competitor X has introduced a new "Growth" tier at $149/mo, positioned between their
> Starter ($49) and Enterprise (custom) plans. The new tier includes SSO and audit logs —
> features previously only available at Enterprise pricing.
>
> **Competitive threat:** This closes a gap that we currently own at the mid-market. Teams
> evaluating us on security features now have a lower-cost alternative from Competitor X.
> Recommend updating our competitive battlecard and flagging for the next sales team sync.

---

## Configuration Reference

| Field | Type | Description |
|---|---|---|
| `sources[].name` | string | Human-readable label for this source (appears in email) |
| `sources[].url` | string | URL to monitor |
| `schedule.cron` | string | Standard cron expression, UTC (default: `"0 6 * * *"`) |
| `email.from` | string | Sender address |
| `email.to` | list | Recipient addresses |
| `email.provider` | string | `"smtp"` or `"sendgrid"` |
| `smtp.host` | string | SMTP hostname |
| `smtp.port` | int | SMTP port (587 for STARTTLS, 465 for SSL) |
| `smtp.use_tls` | bool | Whether to use STARTTLS (default: `true`) |
| `database.path` | string | SQLite file path (default: `"argus.db"`) |

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Always | Anthropic API key for summarization |
| `SMTP_USER` | When `provider: smtp` | SMTP login (usually the sending address) |
| `SMTP_PASSWORD` | When `provider: smtp` | SMTP password or Gmail App Password |
| `SENDGRID_API_KEY` | When `provider: sendgrid` | SendGrid API key |

---

## Extending Argus

**Add a new source:**
Add an entry to `sources` in `config.yaml` and restart (or run `python main.py run` once to capture the baseline).

**Change the schedule:**
Update `schedule.cron` in `config.yaml`. Standard cron syntax, UTC.
```yaml
schedule:
  cron: "0 8 * * 1-5"  # 8 AM UTC, weekdays only
```

**Switch to SendGrid:**
1. `pip install sendgrid`
2. Add `SENDGRID_API_KEY=SG.xxx` to `.env`
3. Set `email.provider: sendgrid` in `config.yaml`

**Inspect the run history:**
```bash
sqlite3 argus.db "SELECT run_at, urls_checked, urls_changed, email_sent FROM run_log ORDER BY run_at DESC LIMIT 10;"
```

**Tune the summarizer prompt:**
Edit `_SYSTEM_PROMPT` in `argus/summarizer.py`. The prompt is designed for B2B SaaS GTM teams — adjust the framing for your context.

**Run as a background service (Linux):**
```bash
# Create a systemd unit — Argus will restart automatically on failure
sudo systemctl edit --force argus
```

Or use a simple cron job to call `python main.py run` daily if you prefer not to run the built-in scheduler as a long-lived process.

---

## Requirements

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com)
- SMTP access (Gmail works) or a [SendGrid](https://sendgrid.com) account

---

## License

MIT
