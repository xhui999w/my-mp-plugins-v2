# 115 Cloud Transfer Assistant

<p align="center">
  <strong>A MoviePilot V2 plugin that monitors public Telegram channels and transfers resources to 115 Cloud</strong>
</p>

<p align="center">
  <a href="README.md">简体中文</a> · English
</p>

<p align="center">
  <img src="https://img.shields.io/badge/MoviePilot-V2-673AB7" alt="MoviePilot V2">
  <img src="https://img.shields.io/badge/Version-1.5.0-00AEEF" alt="Version 1.5.0">
  <img src="https://img.shields.io/badge/Language-Python-3776AB" alt="Python">
</p>

**115 Cloud Transfer Assistant** periodically scans selected public Telegram channels, detects 115 share links, and transfers them to a chosen folder in your 115 Cloud account. It can also detect magnet and ED2K links and submit them as 115 offline-download tasks.

A Telegram Bot is not required for reading channels. The Bot Token is optional and is used only for result notifications.

## How it works

```text
Public Telegram channel
          ↓
Read text, buttons, and hyperlinks
          ↓
Validate the domain and link type
          ↓
115 transfer / HDHive OpenAPI unlock / 115 offline download
          ↓
Save history, deduplicate, count, and notify
```

## Features

- Monitors multiple **public Telegram channels** on a configurable schedule.
- Detects direct 115 share links in message text.
- Reads the actual URL behind labels such as “Click to open” or “Direct link”.
- Supports `115.com`, `115cdn.com`, and `anxia.com` share URLs.
- Preserves extraction codes included in share URLs.
- Optionally detects `magnet:` and `ed2k://` links and submits them to 115 offline download.
- Integrates with MoviePilot subscriptions and can transfer only missing resources.
- Stores processing history in SQLite to prevent duplicate transfers.
- Can send success, failure, and scan-summary notifications through a Telegram Bot.
- Reuses the MoviePilot proxy for Telegram pages and the Bot API.
- Shows status, daily statistics, and recent records on the plugin details page.
- Resolves `hdhive.com/resource/...` through the official HDHive OpenAPI and reuses the existing 115 transfer flow.
- Refreshes expired HDHive access tokens and stores separate pending, successful, and failed records.
- Actively reads MoviePilot subscriptions and queries HDHive by media type and TMDB ID.
- Supports a self-hosted OAuth gateway so App Secret and tokens stay outside MoviePilot.
- Automatically builds linux/amd64 and linux/arm64 Docker images.

## Safe redirect policy

The plugin does not blindly click buttons or browse arbitrary redirect pages.

| Link type | Action |
| --- | --- |
| `115.com`, `115cdn.com`, `anxia.com` | Detect and transfer |
| Trusted parameters on official Telegram domains | Parse the parameter only; do not open the page |
| `magnet:` and `ed2k://` | Submit when offline download is enabled |
| Ad domains, unknown short links, and unknown redirects | Do not visit or execute |
| JavaScript redirects | Do not execute |

Telegram wrapper links are limited to the `url`, `target`, `redirect`, `redirect_url`, and `link` query parameters. This supports common jump-link labels while reducing the risk of opening advertisements.

## Support matrix

| Scenario | Status |
| --- | --- |
| Public Telegram channels | ✅ Supported |
| Direct 115 links in message text | ✅ Supported |
| Trusted 115 links behind buttons or labels | ✅ Supported |
| Trusted links wrapped by official Telegram URLs | ✅ Supported |
| Magnet and ED2K links | ✅ Optional |
| MoviePilot missing-subscription filtering | ✅ Optional |
| Private Telegram channels | ❌ Not supported |
| Ads, unknown short links, and unknown redirects | ❌ Intentionally rejected |
| HDHive OpenAPI unlock and 115 transfer | ✅ Requires an approved app and OAuth authorization |

> The plugin uses only the official HDHive OpenAPI. It does not emulate a login or bypass access controls.

## Installation

### 1. Add the plugin repository

Open the custom repository settings in the MoviePilot V2 plugin market and add:

```text
https://github.com/xhui999w/my-mp-plugins-v2
```

### 2. Refresh the market

Search for **115 Cloud Transfer Assistant** (`115网盘转存助手`) and confirm that version `1.5.0` or later is displayed.

### 3. Install

Select **Install locally**, then open the plugin from the installed-plugin list.

If the market reports success but the plugin is missing:

1. Refresh the browser page.
2. Restart MoviePilot.
3. Install the plugin again.
4. Search the MoviePilot logs for `Tg115Transfer` or `115转存助手`.

## Beginner setup

### Step 1: Add public channels

Enter channel usernames only. Do not include the full URL or the `@` prefix.

For:

```text
https://t.me/example_channel
```

enter:

```text
example_channel
```

Use one channel per line.

### Step 2: Add your 115 Cookie

Sign in to your own 115 account in a browser, obtain the complete Cookie, and paste it into **115 Cookie (required)**.

The Cookie is a sensitive login credential:

- Never post it in a group, GitHub Issue, or log screenshot.
- Never commit it to GitHub.
- Obtain and save a new Cookie when the old one expires.

### Step 3: Set the destination folder ID

Open the destination folder in the 115 web interface. Find the `cid` parameter in the browser address bar and enter its value as the **Destination folder ID**.

Use `0` for the root folder.

### Step 4: Enable optional features

- Normal 115 share transfer requires no additional switch.
- Enable **115 offline download (Magnet / ED2K)** for offline tasks.
- Select **Transfer missing subscription resources only** to filter through MoviePilot subscriptions.
- Add a Bot Token and administrator user ID if Telegram notifications are required.

### Step 5: Save and test

1. Enable the plugin.
2. Enable **Run once now** for the first test.
3. Save the configuration.
4. Wait about 10–30 seconds.
5. Open the plugin details page to view statistics and recent records.
6. Search the MoviePilot logs for `115转存助手`.

Example:

```text
【115转存助手】频道 @example 扫描完成，新链接: 2 条
【115转存助手】发现115分享: https://115.com/s/...
【115转存助手】本轮监控完成 | 发现 2 条 | 成功 2 条 | 失败 0 条
```

## Configuration

| Option | Required | Description |
| --- | --- | --- |
| Enable plugin | Yes | Starts scheduled monitoring |
| Run once now | No | Runs one scan after saving, then resets |
| Check interval | Yes | Default: 10 minutes; 5–30 is recommended |
| Maximum items per scan | Yes | Default: 50 |
| Public channel list | Yes | One channel username per line |
| Bot Token | No | Used only for Telegram notifications |
| Administrator user ID | No | Numeric Telegram ID that receives notifications |
| 115 Cookie | Yes | Sensitive 115 login credential |
| Destination folder ID | Yes | `0` means the root folder; otherwise use the folder `cid` |
| Duplicate handling | Yes | `skip` or `reprocess` |
| 115 offline download | No | Enables magnet and ED2K tasks |
| Subscription mode | No | Can limit transfers to missing MoviePilot resources |
| Result notifications | No | Requires both the Bot Token and user ID |

## Troubleshooting

### Nothing happens after saving

Check the following:

1. The plugin is enabled.
2. The channel is public and its username is correct.
3. The MoviePilot container can access `https://t.me/s/channel_username`.
4. The 115 Cookie is still valid.
5. **Run once now** was enabled, or a full check interval has elapsed.
6. Search the logs for `115转存助手`, not only the plugin-market display name.

### A link is detected but the transfer fails

- `405 METHOD NOT ALLOWED`: upgrade to `1.2.6` or later.
- Invalid Cookie: obtain a new Cookie and save it.
- Expired share or incorrect extraction code: verify the share in a browser.
- Invalid destination: check the folder `cid`.
- A previous failure was deduplicated: temporarily set duplicate handling to `reprocess`.

### Magnet or ED2K links do nothing

Enable **115 offline download (Magnet / ED2K)**. A successful submission means the task entered the 115 offline queue; it does not mean the download has already completed.

### A jump link is ignored

The final destination must be a trusted 115 domain, or it must be present in a trusted parameter on an official Telegram URL. Unknown domains, ad pages, and short links are intentionally ignored.

### Can it monitor private channels?

No. The plugin reads the public Telegram page at `t.me/s/...` and does not sign in to a Telegram user account.

## Changelog

- `1.5.0`: added a self-hosted OAuth gateway, Docker image, and active MP-subscription HDHive transfers.
- `1.4.0`: added HDHive OpenAPI unlock, automatic token refresh, a separate destination, and deduplication records.
- `1.3.1`: added a safe redirect allowlist; ads, short links, and unknown redirects are not visited.
- `1.3.0`: added magnet/ED2K detection and 115 offline-download submission.
- `1.2.7`: added support for 115 URLs behind jump-link labels.
- `1.2.6`: fixed HTTP 405 errors from the 115 share-information endpoint.
- `1.2.5`: fixed configuration loss after a run-once scan.
- `1.2.4`: reused the MoviePilot proxy for Telegram and Bot requests.
- `1.2.0`: added MoviePilot missing-subscription filtering.

The complete history is available from the plugin market.

## Project layout

```text
my-mp-plugins-v2/
├── package.v2.json
└── plugins.v2/
    └── tg115transfer/
        ├── __init__.py
        └── requirements.txt
```

## Acknowledgements

This project was created while studying and referencing:

- [mrtian2016/MoviePilot-Plugins](https://github.com/mrtian2016/MoviePilot-Plugins)
- [walkingddd/TgtoDrive](https://github.com/walkingddd/TgtoDrive)

Thanks to the MoviePilot community and the authors of these open-source projects.

## Disclaimer

This project is intended for personal learning and automation only. Process only content that you are authorized to access and store, and comply with applicable laws and the terms of Telegram, 115 Cloud, and the source websites. The project does not provide any content and its author is not responsible for the availability, legality, or consequences of third-party links.
