# Calling Console frontend

Vite + React (JSX) single-page app. Vercel root directory: `production/frontend`.

    npm install
    npm run dev      # http://localhost:5174, /api proxied to http://localhost:5060
    npm run build

Env: see `.env.example`. With `VITE_GOOGLE_OAUTH_CLIENT_ID` empty the login page shows a dev email box.
Production: `vercel.json` rewrites `/api/*` to the Render service; set the real URL at deploy.
