# Options Dashboard: deploy from an iPad

Read-only Upstox dashboard. It places no orders. You only need Safari.

## One-time setup

1. **GitHub:** sign up at github.com, tap **+ > New repository**, name it
   `options-dashboard`, choose **Private**, create it.
2. In the repo, tap **Add file > Upload files** and upload these 4 files
   (from the Files app): `upstox_dashboard.py`, `requirements.txt`,
   `render.yaml`, `README.md`. Tap **Commit changes**.
3. **Render:** sign up at render.com (log in with GitHub), tap
   **New > Blueprint**, pick your `options-dashboard` repo, continue.
4. When asked for `DASH_PASSWORD`, type a long password only you know.
   `SECRET_KEY` is generated automatically. Tap **Apply** and wait for the
   deploy to finish.
5. Open the `https://...onrender.com` link Render shows. Log in. In Safari,
   tap Share > **Add to Home Screen** for one-tap access.

## Every trading morning

1. Generate your Upstox access token the way you normally do (it expires
   daily around 3:30 AM IST).
2. Open the dashboard, log in, paste the token in the box at the top, tap
   **Save**.

## Things to know

- The token is kept in the server's memory only, not saved to disk. If the
  server restarts or sleeps (the free plan typically sleeps after a period
  of no visits), paste the token again. Open the page a few minutes before
  9:15 so it is awake.
- Never put your token or password inside the code or the GitHub repo.
- Confirm lot size on the dashboard before using the position-size box.
- Decision support only. Not advice.
