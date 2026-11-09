# EVWatch

A small Telegram bot that tells you when an EV charger frees up in Singapore.

Pick the locations you care about, arm them from Telegram, and get an alert the moment a charger becomes available. It works with public and private Charge+ sites and with every operator in [LTA DataMall](https://datamall.lta.gov.sg/).

It is one Python file with no dependencies beyond the standard library.

> **Unofficial project.** EVWatch is not affiliated with or endorsed by Charge+ or LTA. Its Charge+ support uses the private API behind the mobile app, which is undocumented and can change without notice. Using it may be against the Charge+ terms of service, so check those first. You are responsible for how you use your own account.

## Features

- Search for locations from Telegram, by name or postal code
- Track as many locations as you like, each with its own watch timer and polling interval
- Alerts when a charger frees up, when a car has finished charging, and when every charger is taken again
- Watch for a set time or with no time limit
- Saved locations and active watches survive restarts
- Optional notifications through [ntfy](https://ntfy.sh/) or a webhook, such as a Home Assistant automation

## What you need

- Docker with Compose
- A Telegram bot token from [@BotFather](https://t.me/BotFather), and your chat ID
- At least one data source:
  - **LTA DataMall:** a free [account key](https://datamall.lta.gov.sg)
  - **Charge+:** your own Charge+ account, plus the API key its app sends (see below)

## Setup

```bash
git clone https://github.com/salvatooo/evwatch.git
cd evwatch
cp .env.example .env
mkdir -p data
```

Edit `.env` and fill in your values, then start it:

```bash
docker compose up -d
```

Your bot will send you a help message when it starts. Send `/find`, `/add` and `/arm` to start your first watch.

`.env` and `data/` hold your credentials. Both are excluded by `.gitignore`, so keep them out of any fork or backup you share.

### Charge+ setup

Set these in `.env`:

- `CHARGEPLUS_PHONE`
- `CHARGEPLUS_PASSWORD`, or `CHARGEPLUS_PASSWORD_FILE`
- `CHARGEPLUS_API_KEY`

`CHARGEPLUS_API_KEY` is the value of the `x-api-key` header the Charge+ app sends when it signs in, and it's the only value you need to capture. Capture it once from your own device. The app is built with Flutter, which ignores the phone's Wi-Fi proxy setting, so you need a VPN-based capture tool. Only do this for your own account.

If you only want LTA data, set `USE_CHARGEPLUS=false`.

## Telegram commands

| Command | What it does |
|---|---|
| `/help` | Show the commands |
| `/find cp <name>` | Search Charge+, including private sites your account can see |
| `/find lta <name, address or postal code>` | Search LTA DataMall |
| `/add <result number>` | Track a location from your latest search |
| `/add <Charge+ ID or URL> [name]` | Track a Charge+ location directly |
| `/locations` | List your locations and their status |
| `/arm <number or name> [minutes] [poll minutes]` | Start watching a location |
| `/arm all [minutes] [poll minutes]` | Start watching every location |
| `/disarm [number or name]` | Stop watching one location, or all of them |
| `/status [number or name]` | Show what's free right now |
| `/setpoll <number or name> <minutes or default>` | Set how often a location is checked |
| `/rmlocation <number or name>` | Stop tracking a location |

Use `-1` as the minutes for a watch with no time limit, for example `/arm 2 -1`.

Adding a location only saves it. It's checked once you arm it.

## Configuration

Everything is set in `.env`. See [`.env.example`](.env.example) for the full list.

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | | Your bot's token |
| `TELEGRAM_CHAT_ID` | | The only chat the bot responds to |
| `LTA_ACCOUNT_KEY` | | LTA DataMall key |
| `USE_LTA` | `true` | Also track the postal codes in `POSTAL_CODES` |
| `POSTAL_CODES` | | Comma-separated postal codes to track |
| `USE_CHARGEPLUS` | `true` | Enable Charge+ |
| `CHARGEPLUS_PHONE` | | Charge+ account phone number |
| `CHARGEPLUS_PHONE_IDD` | `+65` | Country code for that number |
| `CHARGEPLUS_PASSWORD` | | Charge+ account password |
| `CHARGEPLUS_PASSWORD_FILE` | | Read the password from a file instead |
| `CHARGEPLUS_API_KEY` | | The app's `x-api-key` value, needed to sign in |
| `CHARGEPLUS_URL` | | Optional comma-separated status URLs for the first start |
| `POLL_SECONDS` | `120` | Default polling interval, with a minimum of 120 |
| `MODE` | `manual` | `manual` uses `/arm`; `always` watches every location |
| `ARM_DEFAULT_MINUTES` | `180` | Watch length when none is given |
| `ACTIVE_HOURS` | | `always` mode only, for example `07-23` |
| `NOTIFY_ON_FINISHING` | `true` | Alert when a car has finished charging |
| `NOTIFY_ON_TAKEN` | `false` | Alert when every charger is taken again |
| `PLUG_TYPES`, `CURRENT`, `MIN_KW`, `STATION_FILTER`, `OPERATOR` | | Limit which chargers count |
| `NTFY_URL`, `WEBHOOK_URL` | | Extra notification targets |

## Good to know

- **Polling is limited to once every 2 minutes per location.** Please don't lower this. Polling harder puts load on these services and can get your account blocked.
- **Charge+ allows one session per account.** When EVWatch signs in, your phone is signed out, and signing in on your phone interrupts EVWatch. Send `/arm` to let it sign back in. It also signs in by itself when its token nears expiry, about every 47 hours.
- **Long watches add up.** A location on the 2-minute default makes 720 requests a day, so use a longer interval for anything you leave running.
- **LTA data can lag.** LTA refreshes its availability feed periodically, so alerts may arrive a few minutes after a change.

## Updating the code

`evwatch.py` is mounted into the container, so after changing it you only need:

```bash
docker compose restart
```

After changing `.env`, recreate the container instead:

```bash
docker compose up -d
```

## License

[MIT](LICENSE)
