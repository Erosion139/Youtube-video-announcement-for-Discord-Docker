# YouTube → Discord Notifier

A small Docker app that watches YouTube channels and posts each new upload to Discord through your own bot. Everything is set up from a web page on port **25599**: the bot token, the Discord channel to post in, and the list of YouTube channels, each with its own on/off switch and optional message (for example, `Duckman uploaded a video! QUACK!`).

## How new uploads are found

The app reads each channel's public YouTube feed on a schedule you choose (every 5 minutes by default). This needs no YouTube API key and uses no quota.

It can also receive **instant notifications**: YouTube pushes new uploads to the app within seconds through WebSub (PubSubHubbub). This only works when YouTube can reach the app from the internet; see [Instant notifications](#instant-notifications). The scheduled check keeps running either way, so nothing is missed if a push never arrives.

When you add a channel, the videos already on it are recorded silently. Only uploads that appear after that are announced, and each video is posted once.

---

## 1. Publish the image from your GitHub account

This repository contains a GitHub Actions workflow that builds the Docker image and publishes it to the GitHub Container Registry each time you push to `main`.

1. On GitHub, create a new repository named `yt-discord-notifier`. Leave it empty (no README or license). Public is simplest; see step 4 if you make it private.
2. Push these files to it from the folder that contains this README:

   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR_GITHUB_USERNAME/yt-discord-notifier.git
   git push -u origin main
   ```

   If you would rather upload through the GitHub website, make sure the hidden `.github/workflows/docker-publish.yml` file comes along; without it nothing gets built. If it goes missing, use **Add file → Create new file**, type `.github/workflows/docker-publish.yml` as the name, and paste its contents.
3. Open the **Actions** tab. The "Build and publish Docker image" run takes a few minutes. When it turns green, the image is at `ghcr.io/YOUR_GITHUB_USERNAME/yt-discord-notifier:latest`, built for both `amd64` and `arm64`.
4. On your GitHub profile, open **Packages → yt-discord-notifier → Package settings**. If the visibility is Private, change it to Public so TrueNAS can download it without a login. (To keep it private instead, create a GitHub personal access token with the `read:packages` scope and add it in TrueNAS under **Apps → Configuration → Manage Container Image Registries** for `ghcr.io`.)

To release a numbered version as well as `latest`, push a tag such as `v1.0.0`.

## 2. Install on TrueNAS SCALE

These steps are for TrueNAS SCALE 24.10 (Electric Eel) or newer, which runs apps with Docker.

1. **Create a dataset for the app's data.** Under **Datasets**, add a dataset such as `apps/yt-discord-notifier` and choose the **Apps** preset. The container runs as the `apps` user (UID/GID 568), so that user must be able to write to it. If you use an existing folder, set its owner to user `apps` and group `apps` under **Edit Permissions**.
2. **Install the app.** Go to **Apps → Discover Apps**, open the **⋮** menu and choose **Install via YAML**. Name it `yt-discord-notifier` and paste the contents of `docker-compose.yml`, replacing:
   - `YOUR_GITHUB_USERNAME` with your GitHub username in lowercase
   - `/mnt/YOUR_POOL/apps/yt-discord-notifier` with the dataset path from step 1
3. Save. Once the app shows as running, open `http://YOUR_TRUENAS_IP:25599`.

To require a login for the web page, uncomment `ADMIN_PASSWORD` in the YAML and set a password. The username is `admin`.

If you prefer the **Custom App** form over YAML, the settings are: image `ghcr.io/YOUR_GITHUB_USERNAME/yt-discord-notifier`, tag `latest`, port `25599` → `25599`, a host-path volume from your dataset to `/data`, and user/group `568`.

### Updating

Push your changes to GitHub and wait for the Actions run to finish. TrueNAS then offers an update for the app, which pulls the new `latest` image. Your settings live in the dataset and are kept.

## 3. First-time setup in the web interface

The banner at the top of the page always shows the next step.

1. **Create the bot.** In the [Discord Developer Portal](https://discord.com/developers/applications), choose **New Application**, open the **Bot** page, choose **Reset Token** and copy the token. No privileged intents are needed.
2. **Save the token** in the "Discord bot" panel. The status turns green when the bot is online. The token is stored in the app's database and is never shown again in full.
3. **Add the bot to your server** with the "Add the bot to a server" button. It asks only for View Channel, Send Messages, Embed Links and Mention Everyone.
4. **Choose where to post** from the "Post new videos in" list, then press **Send test message**. You can also paste a channel ID or a channel link (turn on Developer Mode in Discord under Settings → Advanced, then right-click the channel).
5. **Add YouTube channels** by pasting a channel link, an `@handle`, a channel ID, or even a link to one of the channel's videos.

### Per-channel options

Each channel in the list has an on/off switch. Expand a channel to set:

- **Message**: text posted before the video link. `{channel}`, `{title}` and `{url}` are filled in; if `{url}` isn't used, the link goes on its own line below the message. Channels without a message use the default message. You can mention a role with `<@&ROLE_ID>` or use `@everyone`.
- **Discord channel**: post this YouTube channel somewhere other than the default.
- **Include Shorts**: on by default. Turn it off to announce only regular videos.
- **Test post**: sends the channel's latest video with your message, so you can see how it looks. This doesn't affect what counts as new.

Turning a channel off and back on doesn't flood Discord with what was uploaded in between; the app records those videos silently and continues from there.

### Checking settings

- **Check every**: how often each feed is read, in minutes.
- **Skip videos older than**: videos published longer ago than this are never announced, for example when a creator makes an old video public. Set to 0 to turn this off.

## Instant notifications

YouTube's WebSub hub can push uploads to the app as they happen, but only if it can reach `/websub/callback` from the internet.

1. Expose the app through a reverse proxy or a tunnel such as Cloudflare Tunnel. Only the `/websub/` path needs to be public, and it only accepts properly signed updates for channels you've added.
2. In the web page, open **Instant notifications** and enter the public address, for example `https://yt.example.com`.

The app subscribes each channel, renews the subscriptions before they expire, and shows how many channels are receiving pushes. If you'd like the rest of the web page to be reachable from outside too, set `ADMIN_PASSWORD` first.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `25599` | Port the web interface listens on inside the container |
| `DATA_DIR` | `/data` | Where the database is stored |
| `ADMIN_PASSWORD` | *(empty)* | When set, the web page asks for a login |
| `ADMIN_USER` | `admin` | Username for that login |
| `LOG_LEVEL` | `INFO` | `DEBUG` for more detail in the container logs |
| `TZ` | `UTC` | Time zone used in the logs |

Everything else is configured in the web interface.

## Troubleshooting

- **The app stops right after starting and the logs mention `/data`**: the dataset isn't writable by UID/GID 568. Set its owner to `apps`:`apps`.
- **TrueNAS can't pull the image**: the package is still private (see step 1.4), or the username in the image name isn't lowercase.
- **"Missing Access" or "Missing Permissions" when posting**: the bot can't see or post in that Discord channel. Check the channel's permission overrides for the bot's role.
- **A channel shows an error**: the message next to it explains what failed. The app retries on every check, and a video whose post failed is retried until it goes through.
- The **Activity** panel and the container logs show every check, post and error.
