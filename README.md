# nidhogg-newswire

A small, free news watcher. Every 5 minutes GitHub runs `poll.py`, which **reads** a few public
websites, writes down anything that matches your watchlist, and sends the most important matches
to your phone through the free ntfy app.

It only reads public pages and sends phone alerts. There is **no trading code, no logins to any
exchange or brokerage, and no connection to any account.**

## What it reads

| Source | What it looks at |
|---|---|
| SEC EDGAR | the newest 100 8-K filings |
| Federal Register | the newest 20 documents (presidential documents, plus anything mentioning tariff, export control, sanction, gallium or germanium) |
| BBC Business, NYT Business | their business news RSS feeds (times converted from GMT to US Eastern) |
| FAA | airport status (ground stops, delays, closures) |
| National Weather Service | active alerts marked Extreme |
| Kalshi (public list only) | the names of companies that have a Kalshi earnings-mention series |

It never uses Cboe. It identifies itself honestly to every site. If a site says no (HTTP 403 or 429),
it skips that site for that run and writes that down. It never tries to get around a block.

## The two tiers

- **Tier 1: your phone buzzes.** An 8-K or news story naming Dell, Deere, Nvidia, Micron, ASE
  Technology, EMCOR, Analog Devices, Hewlett Packard Enterprise, HP Inc, Marvell, Lumentum, SanDisk
  or NetApp, or any item with the words tariff, export control, sanctions, gallium, germanium,
  ground stop, strike or outage.
- **Tier 2: written down quietly, no buzz.** Any 8-K or news story naming a company that has a
  Kalshi earnings-mention series, and any 8-K with item 2.02, 1.01, 5.02 or 8.01. Presidential
  documents and NWS Extreme alerts that don't hit a tier 1 word also land here.

If 3 or more sources fail in one run, you get one tier 1 push saying so (at most once an hour).

## The files

| File | What it is |
|---|---|
| `poll.py` | the program. Runs once and stops. |
| `watchlist.json` | your lists of companies, keywords and 8-K items. Edit this to change what it watches. |
| `test_items.json` | 10 real items from 25 Nov 2026, used only by the test mode |
| `.github/workflows/poll.yml` | tells GitHub to run `poll.py` every 5 minutes |
| `seen.json` | made automatically. Remembers what was already reported so you never get the same alert twice. |
| `alerts.jsonl` | made automatically. One line per match: the item's own time, the time we saw it, tier, source, title, link. Failed sources are logged here too, with tier `error`. |

The very first run only remembers what is already out there. It does not send alerts or log those
items, so you won't get a flood of old news.

## Commands (if you ever run it on a computer)

```
python3 poll.py              # normal run
python3 poll.py --dry-run    # fetch and print only; sends nothing, writes nothing
python3 poll.py --test       # runs the 10 test items through the rules and sends ONE test push
python3 poll.py --last24h    # prints the last 24 hours of tier 2 matches
```

The phone push needs the topic name in the `NTFY_TOPIC` setting. On GitHub, that's a repo secret.
The topic name is never written in this repo.

sec.gov asks every visitor to include a contact email in its User-Agent. That text comes from the
`SEC_USER_AGENT` setting, a GitHub repo secret, so no email is written in this repo. Without it,
the program sends just `nidhogg-research`, and sec.gov may refuse. That refusal shows up as a failed
source in the log.

## How to add a company

Open `watchlist.json` on GitHub, click the pencil (Edit), and:

- **For a phone alert (tier 1):** add a line inside `"companies"`, for example
  `"Intel": ["Intel"],`. The list holds every spelling to look for, e.g. `["Hewlett Packard Enterprise", "HPE"]`.
- **For the quiet log (tier 2):** add the name inside `"extra_companies"`, for example
  `"extra_companies": ["Intel", "Texas Instruments"]`.
- **A new keyword:** add it to `"keywords"`. Plurals are matched automatically (tariff matches tariffs).

Watch the commas: every line in a list except the last needs one. Click **Commit changes**, and the
next run uses the new list.

## How to turn it off

- **Pause:** on GitHub, open the **Actions** tab, click **poll** on the left, then click the **...**
  button and choose **Disable workflow**. Choose **Enable workflow** to start it again.
- **Stop phone alerts only:** delete the `NTFY_TOPIC` secret (Settings, then Secrets and variables, then Actions).
  It keeps logging quietly.
- **Remove it completely:** Settings, scroll to the bottom, **Delete this repository**.

Note that GitHub often starts "every 5 minutes" jobs late, sometimes by 10 to 30 minutes when it's
busy. GitHub also pauses scheduled jobs in a repo that has had no commits for 60 days. If that
happens, re-enable it in the Actions tab.
