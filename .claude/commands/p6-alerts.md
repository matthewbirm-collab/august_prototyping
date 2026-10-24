---
description: Phase 6 — users, watchlists and projection alerts
---
Follow the working agreement in `CLAUDE.md`. Roadmap step: `/p6-alerts`.

- Authentication via a managed provider (email magic link or OAuth). Ask me which provider before starting.
- "My players" watchlist that users build manually.
- Alerts when a watched player's projection moves more than `ALERT_THRESHOLD_PTS` or a steam event hits one of his markets. Channels: email and web push.
- Batch per user (one digest per `ALERT_DIGEST_MIN` minutes), respect quiet hours, include the driver prop and context in each alert.
- Store alert history; let users mute a player.
