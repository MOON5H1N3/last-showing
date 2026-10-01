# Last Showing

Plans your Monzo Vue tickets around your own taste, so you catch the films you'll love before their last showing.

Each month it works out which films to use your tickets on and when you need to go by, and sends the plan to you by Discord DM. The dashboard shows the same plan, what's leaving soon, the next two months, every film scored for you, and what it has learned about your taste.

Not affiliated with Vue Entertainment or Monzo.

## How it decides

1. **What's on.** Every morning it reads your Vue's listings, including Vue's own coming-soon list, which runs months ahead.
2. **What you'd think of it.** It learns from your Letterboxd ratings which directors, writers, actors, genres, themes and studios you rate above or below your average, and how much each of those matters *for you*. It also considers the Letterboxd community average, trusting it less when it's based on only a few early ratings. A film with no Letterboxd average yet is scored on your taste alone, rather than treated as an average film. The dashboard shows how accurate its guesses are on films you've already rated.
3. **How long it'll be on.** Concerts and re-releases have fixed dates. For everything else it learns from real history: past listings of 5 suburban London Vues since November 2025, from [Clusterflick](https://github.com/clusterflick) (CC BY 4.0), plus your own cinema's finished runs as they build up. For each film it finds similar past situations (weeks since opening, share of the cinema's showings, how far its showings have fallen) and counts how many of those films were still on later. When Vue stops listing a film before the end of the published week, that's taken as a sign it's leaving.
4. **The trade-off.** A film you'd love that will still be showing next month can wait for next month's tickets. So each film's priority is its predicted rating, plus a lift if it has a better-than-usual chance of being a 4.5★+ favourite, plus a lift if it suits the big screen (spectacle genres, EPIC or IMAX showings, concerts and theatre), minus a penalty for how likely it is to still be around next month. Quieter films you'd enjoy just as much at home are listed under "Catching later" instead of competing for your tickets. The sliders on the Settings page set those sizes.

**Buzz** is shown on every film page: how many Letterboxd lists it's on, how many members have seen it early, likes, TMDB popularity, and how many showings your Vue gave its opening week, compared with everything else at your cinema. Buzz doesn't change the predicted rating. It's logged over time so that, once enough of those films are rated, the Taste page can say whether pre-release buzz predicts how much you'll like a film. It already says whether you rate big films differently from the crowd.

The **Taste** page's "How well it works" section shows both models tested on films they hadn't seen: rating predictions on a held-out fifth of your ratings, and run lengths on held-out films.

Ticket tracking works in two ways. It's automatic: a film you log in your Letterboxd diary that was showing at your Vue that day counts as a used ticket, up to your monthly allowance. You can also mark or undo tickets yourself, from the dashboard or with Discord's buttons and `/used`.

## Setup (about 15 minutes)

### 1. Get a TMDB key (free)
Make an account at [themoviedb.org](https://www.themoviedb.org/signup), then go to **Settings → API** and request a developer key. Copy the **API Key**; the longer **API Read Access Token** works too.

### 2. Make the Discord bot
1. Go to the [Discord developer portal](https://discord.com/developers/applications) and choose **New Application**. Call it Last Showing.
2. Open **Bot**, choose **Reset Token**, and copy the token. That's `DISCORD_BOT_TOKEN`.
3. Open **OAuth2 → URL Generator**. Tick `bot` and `applications.commands`, open the generated link, and add the bot to a server you're in. Your own private server is fine. The bot needs to share a server with you to be allowed to DM you.
4. In Discord, turn on **Settings → Advanced → Developer Mode**. Then right-click your own name and choose **Copy User ID**. That's `DISCORD_USER_ID`.
5. If DMs don't arrive, right-click the server icon, open **Privacy Settings**, and allow direct messages from server members.

### 3. Export your Letterboxd data
On letterboxd.com, go to **Settings → Data → Export your data**. Keep the .zip; you'll add it in step 5.

### 4. Configure and start it
Put this folder somewhere permanent, e.g. `C:\docker\last-showing`. Then, in PowerShell in that folder:

```powershell
copy .env.example .env
notepad .env          # fill in TMDB_API_KEY, DISCORD_BOT_TOKEN, DISCORD_USER_ID, LETTERBOXD_USERNAME
docker compose up -d --build
```

The first build takes a few minutes, because it downloads a headless Chromium to read Vue's listings.

To run it with your arr stack instead, copy the `last-showing` service from `docker-compose.yml` into that stack's compose file. Point `build:` at this folder and `env_file:` at this folder's `.env`.

### 5. Add your Letterboxd export
Open **http://localhost:8095**, go to **Settings → Letterboxd**, and upload the export .zip. (You can also drop it into `data\letterboxd\`.) Matching your ratings to TMDB takes a minute or two the first time. After that, only new films are looked up.

Re-upload an export every few months to pick up old ratings you've changed. New diary entries arrive automatically through your public RSS feed.

## Using it

| Page | What's there |
|---|---|
| **Plan** | Four parts, each film shown once: your ticket picks; worth paying for (your paid trips, or a few optional extras, big-screen films first); catching later (films just as good at home, with roughly when they can be rented); and everything else, folded away in date order. Click the month name (or the month pills) to look at the next two months |
| **Films** | Everything showing or coming soon, scored for you. Sort by best for you, leaving soonest or opening date |
| **Film page** | Trailer, predicted rating with the reasons for and against, the chance it's still on in a week, two weeks and a month, and showtimes by day |
| **Taste** | What it's learned about you, and how accurate it is on films it hadn't seen |
| **Settings** | Tickets, cinema, recommendation sliders, Letterboxd, Discord, schedule, backups, and films you've ruled out |

On any film: **Used a ticket**, **I'm seeing this** (pins it to a month and keeps a ticket for it) and **Not for me** (never suggested again). The same buttons come with the monthly Discord DM, and `/picks`, `/used`, `/undo` and `/refresh` work in your DMs with the bot.

## Settings

Everything except secrets is on the Settings page and takes effect when you save:

- **Tickets:** Monzo Perks (1 a month), Custom (any number, e.g. 2 if you have a spare), or No tickets. Also expiry (end of month or roll over one month), and showings your tickets don't cover (EPIC, 3D, Ultra Lux).
- **Cinema:** any Vue in the UK.
- **Recommendations:** best film first vs never miss anything, big screen vs fine at home, paid trips a month (default 0), how hard to chase likely favourites, watchlist boost, and whether to include events, re-releases and films you've seen.
- **Letterboxd, Discord, schedule and data.**

`.env` holds the secrets (TMDB key, Discord token and user ID, dashboard password) and first-run defaults. Change `.env` only for secrets, then run `docker compose up -d`.

## Updating

From your Last Showing folder, in PowerShell:

```powershell
.\update.ps1
```

It downloads the latest version from GitHub, replaces the program files and rebuilds the container. Your `.env`, your `data` folder and your `docker-compose.yml` are never touched. If Windows refuses to run it, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once. The version you're running is shown at the bottom of every page.

Coming from Vuearr? Your data carries over automatically. Because the container name changed, run `docker compose down` once before your first update.

## Running it for someone else

Nothing is hard-coded to one person or cinema. A friend runs their own copy with their own `.env` secrets, then picks their Vue, tickets and Letterboxd username on the Settings page.

## Troubleshooting

- **"Vue listings" fails in the refresh log.** Vue may have changed its site. The listings are read the same way the website reads them, from `/api/microservice/showings/cinemas/<id>/films`. If the cinema ID lookup fails, set `VUE_CINEMA_ID` (Cribbs Causeway is `10018`).
- **"Letterboxd averages" fails.** Letterboxd sometimes blocks automated page loads. Predictions fall back to TMDB's average, which is less informative but still works. You can set `LETTERBOXD_COMMUNITY_RATINGS=false` to stop trying.
- **A film is matched to the wrong TMDB entry,** or a real film sits under "Couldn't score". Paste the right TMDB link into its row on the dashboard.
- **Logs.** Run `docker logs -f last-showing`.

Data lives in `data\lastshowing.sqlite3`, with daily copies in `data\backups`. Deleting it starts fresh (keep the export .zip).

## Development

```
pip install -r requirements.txt
python -m unittest discover tests
DATA_DIR=./data python -m app.main
```
