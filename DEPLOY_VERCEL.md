# Vercel Deployment

This folder is ready to upload to GitHub and import directly into Vercel.

## 1. Create a GitHub repository

Upload **the contents of this folder** to the repository root:

```text
app.py
core.py
templates/
requirements.txt
.python-version
.env.example
.gitignore
README.md
DEPLOY_VERCEL.md
```

Do not upload `config.local.json` or any real API keys.

## 2. Import in Vercel

1. Open Vercel.
2. Choose **Add New → Project**.
3. Import the GitHub repository.
4. Vercel should detect the Flask application automatically.
5. Before the final deploy, add Environment Variables.

## 3. Environment Variables

Required:

```text
KTO_SERVICE_KEY
KMA_SERVICE_KEY
KAKAO_REST_API_KEY
GEMINI_API_KEY
GEMINI_MODEL=gemini-3.6-flash
```

For your current project, set `KMA_SERVICE_KEY` separately using the KMA key
that you already confirmed works.

Optional:

```text
KMA_KEY_MODE
SEOUL_OPEN_DATA_KEY
YEOBAEK_CONTACT
```

If `KMA_KEY_MODE` is omitted, Yeobaek can diagnose a working KMA key transport
mode on the first weather request.

## 4. Deploy

Click **Deploy**.

When finished, open the Vercel domain:

```text
https://YOUR-PROJECT.vercel.app
```

Check:

```text
https://YOUR-PROJECT.vercel.app/api/health
```

A healthy deployment returns JSON containing:

```json
{
  "ok": true,
  "service": "yeobaek",
  "version": "vercel-1.0.3",
  "deployment": "vercel"
}
```

## 5. GPS

Vercel provides HTTPS automatically, so browser geolocation can work on
supported mobile browsers after the user grants permission.

## 6. Updating API keys

Change them in:

**Vercel → Project → Settings → Environment Variables**

Then redeploy the project so the new values are used.

## Local preview

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Run:

```bash
flask --app app run --debug
```

Open:

```text
http://127.0.0.1:5000
```

Or use Vercel CLI:

```bash
vercel dev
```


## Real API connection check

`/api/health` only tells you whether environment-variable values exist.

After redeploying, open:

```text
https://YOUR-PROJECT.vercel.app/api/service-check
```

This performs real test calls for:
- TourAPI
- KMA weather
- Kakao transit

and reports Gemini configuration without spending an AI generation request.

The response never exposes the secret values. It only shows secret length and
a short SHA-256 fingerprint so you can confirm that Production is using the
same value you intended to save.

If TourAPI reports `SERVICE_KEY_IS_NOT_REGISTERED_ERROR`, check the Vercel
`KTO_SERVICE_KEY` value. Paste the key only, without quotes and without
`KTO_SERVICE_KEY=`. Version 1.0.1 also automatically strips those common
copy/paste mistakes and tries both Encoding and Decoding request forms.
