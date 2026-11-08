# Deploy Rukkoon to Vercel and Supabase

This setup starts with an empty database. Vercel runs the Python app and admin
page; Supabase stores bookings in Postgres and slips in private Storage. LINE
sends messages to the Vercel webhook. Docker and Caddy are not used on Vercel.

## 1. Create Supabase

1. Create a project at https://supabase.com/dashboard. Save its database password
   in your password manager. Choose a region near your customers and use a nearby
   Vercel function region where your plan supports it.
2. Open SQL Editor, paste the full contents of `supabase/schema.sql`, and run it.
   This creates the private `booking` schema and private `booking-slips` bucket.
   Do not expose the `booking` schema through the Data API or make the bucket public.
3. In Connect, select the **Transaction pooler** connection string (port 6543).
   Replace the password placeholder with your URL-encoded database password.
   Add `?sslmode=require`. Copy the exact host and username supplied by Supabase.
4. From the project settings, obtain the project URL and **service_role** API key.
   These belong only in Vercel's server environment, never in HTML or browser code.

## 2. Create Vercel

1. Put this project in a private Git repository and import it in Vercel.
   Never commit a populated `.env` file. Use the project root, not `dist/`.
2. Choose the **Other** framework preset. Leave build command and output directory
   unset. The Python function is `api/index.py`; `vercel.json` routes the app to it.
3. Add every variable from `.env.vercel.example` in Project Settings → Environment
   Variables for **Production**. Generate separate random values of at least
   32 characters for `ADMIN_PASSWORD` and `CRON_SECRET`.
4. Get `LINE_CHANNEL_SECRET` and `LINE_CHANNEL_ACCESS_TOKEN` from the Messaging API
   channel linked to @rukkoon. Keep `ALLOW_INSECURE_LOCALHOST=0`.
5. Deploy. Record the stable production domain, for example
   `https://rukkoon-booking.vercel.app`. Do not connect LINE to a temporary preview URL.
6. Make the production webhook reachable without Vercel login. If deployment
   protection intercepts it, configure production access accordingly. The app's
   own admin login remains required for booking administration and slip access.

Use a separate Supabase project and separate LINE test channel for previews.
Do not attach production credentials to arbitrary preview branches.

## 3. Schedule queue processing in Supabase

The app processes LINE events during webhook requests and sends approval messages
during admin requests. A scheduled job is still required to retry unsent messages,
recover interrupted requests, and expire unused reservations when there is no traffic.

In Supabase → Integrations → Cron, enable Cron (and pg_net if prompted), then create:

- Name: `rukkoon-worker`
- Schedule: `* * * * *` (every minute)
- Type: HTTP request
- Method: GET
- URL: `https://YOUR-PRODUCTION-DOMAIN/api/cron`
- Header: `Authorization: Bearer YOUR_CRON_SECRET`
- Timeout: 60 seconds if the interface provides this setting

Use the same secret as Vercel's `CRON_SECRET`. Check job history and HTTP response
status, not only whether the scheduler submitted the request. The endpoint should
return JSON with `ok: true`; without the bearer secret it must return 401.

Inventory expires at the 15-minute deadline on the next inventory request or job
run. This replaces the old 0.5-second background loop. A database lease prevents
overlapping workers; a terminated worker can be recovered after two minutes.
Queued replies with expired LINE reply tokens use Push API fallback, which may
consume the OA's message allowance. Failed business events appear in the admin
error count and require inspection; they are not blindly replayed.

## 4. Check the deployment before connecting LINE

1. Open `/health` (liveness check only).
2. Open `/`, log in under เชื่อมต่อ LINE, and load the admin dashboard. This verifies
   database access too. Under ตั้งค่าการจอง, fill in the real bank details.
3. Confirm `/api/admin` and `/slips/example.jpg` reject requests when logged out.
4. Verify the Cron HTTP request succeeds and no credentials appear in browser code.

## 5. Connect LINE

In LINE Developers → the @rukkoon Messaging API channel:

1. Set Webhook URL to `https://YOUR-PRODUCTION-DOMAIN/webhook/line`.
2. Click Verify; enable Use webhook and Webhook redelivery.
3. Adjust the OA's automatic replies so the bot does not send duplicate replies.
4. Add the OA as a friend and send `จองบัตร`.

Open bookings only for a controlled test first. Complete booking → upload slip →
admin approval → LINE confirmation → check-in. Also test rejection, expiry, two
customers selecting the same table, logout, and a JPG/PNG slip close to 10 MB.
Slips are downloaded from LINE server-to-server, then shown to authenticated
admins through a short-lived signed Storage URL; large files do not pass through
Vercel's HTTP response body limit.

Do not announce sales until these live checks pass. Local SQLite tests do not
establish Postgres concurrency behavior or live provider connectivity.

## Operational notes

- Back up the Postgres database **and** Storage objects; they are separate assets.
- Monitor failed events and unsent messages in the dashboard and Vercel logs.
- Booking writes use a transaction-level advisory lock across all app instances.
  This favors inventory correctness over maximum throughput for this single event.
- Database schema changes are manual SQL migrations, never automatic cold-start DDL.
- The code is prepared for deployment; creation of the two cloud projects,
  installation of secrets, live Postgres tests, and LINE end-to-end verification
  are still required.

Provider references:
- https://vercel.com/docs/functions/runtimes/python
- https://supabase.com/docs/guides/database/connecting-to-postgres
- https://supabase.com/docs/guides/storage/security/access-control
- https://supabase.com/docs/guides/cron/quickstart
