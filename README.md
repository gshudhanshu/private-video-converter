# Private Video Converter MVP

A single-container, one-job-at-a-time dashboard for converting videos stored in Google Drive.

## Important limitations

- This is an MVP for controlled testing, not yet a production service.
- Job history is in memory and is lost when the container restarts.
- The worker uses a local temporary MP4, uploads it, verifies remote file size, and deletes the local output only after verification.
- If upload or verification fails, the local output is retained for recovery.
- Google Drive source mount is read-only. Source files are never deleted or modified.
- The 20 GB free-space guard is only a basic safeguard, not proof that a large conversion will fit.
- Do not run full conversions of 180 GB source files until a representative long-file test and disk/cache monitoring are completed.
- The dashboard uses HTTP Basic Auth. Put it behind HTTPS in Coolify and set a strong unique password.
- FUSE requires `/dev/fuse`, `SYS_ADMIN`, and the AppArmor setting in Compose. These permissions increase container privileges.

## Deploy using Coolify

1. Put this project in a private Git repository.
2. Create/update the Coolify application to deploy from that repository using Docker Compose.
3. In Coolify environment variables, set `APP_USER` and a long random `APP_PASSWORD`.
4. Preserve the existing persistent volume named `rclone-config`. It contains `/config/rclone.conf`.
5. Preserve the `video-work` and `video-cache` volumes if you want continuity. Do not delete the config volume.
6. Ensure the service is exposed on port `8000` through Coolify's proxy with HTTPS.
7. Deploy. The app starts the read-only rclone mount and the dashboard.

## Existing Google Drive setup

Expected remote folders:
- `gdrive:VideoConverter/Input`
- `gdrive:VideoConverter/Converted`

The existing `/config/rclone.conf` must contain the authenticated `gdrive` remote.

## Test procedure

1. Put a small test video in `VideoConverter/Input`.
2. Open the dashboard URL and authenticate with `APP_USER` / `APP_PASSWORD`.
3. Select the video and leave Test duration at 2 minutes.
4. Confirm the output appears in `VideoConverter/Converted`.
5. Confirm the job says upload verified and temporary output deleted.
6. Check `/work/output` and `/cache/rclone` from the container terminal.

## Recommended next hardening

- Create and use a dedicated Google OAuth client ID (the current rclone shared client warning should be resolved).
- Persist job records and implement cancellation/recovery.
- Add checksums or stronger remote verification in addition to size.
- Add free-space estimation and a configurable hard cache budget.
- Add structured logs, alerting, and rate limits.
- Test long MOV files with varied moov atom placement, codecs, and seek behavior.
