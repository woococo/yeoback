# Yeobaek (旅Back) — Vercel Web Edition 1.0.4

여백은 여행 중 생긴 빈 시간과 날씨·혼잡·이동 같은 변수를 분석해
현재 위치에서 다음 일정까지 안전하게 활용할 관광지와 코스를 추천합니다.

This Vercel edition is converted from the working local **Yeobaek 1.1.2**
release without removing the recommendation features.

## Included

- GPS current location
- destination search / map selection
- remaining-time based recommendation
- preference filtering
- KTO tourism information
- KMA nowcast / ultra-short / short forecast
- planned-arrival opening-hours check
- Kakao public-transit time / route
- single-place and two-stop itineraries
- congestion prediction / visitor context
- related attractions
- barrier-free information
- pet information
- official tourism photos
- audio guide
- detailed `더보기`
- Gemini 3.6 Flash AI Travel Agent
- responsive mobile-first Y2K / scrapbook UI

## Vercel architecture

```text
Browser
  ↓
Flask on Vercel
  ├─ /api/recommend
  ├─ /api/weather
  ├─ /api/place-details
  ├─ /api/route
  └─ /api/ai-chat
       ↓
KTO / KMA / Kakao / Gemini
```

The API keys stay on the server as Vercel Environment Variables and are not
embedded in the browser JavaScript.

## Deploy

See:

`DEPLOY_VERCEL.md`

## Important serverless note

The recommendation cache is in memory. Vercel may reuse a warm Flask instance,
but the cache is not permanent and can disappear after a cold start. This does
not remove functionality; it only means an external API may occasionally be
queried again.

## Required environment variables

```text
KTO_SERVICE_KEY
KMA_SERVICE_KEY
KAKAO_REST_API_KEY
GEMINI_API_KEY
GEMINI_MODEL=gemini-3.6-flash
```


## API diagnostics

Use:

`/api/service-check`

instead of relying only on `/api/health`.

`health` = environment value exists  
`service-check` = provider accepted the key and returned a real response


## Fonts

Version 1.0.4 applies `Gmarket Sans` as the **primary font across the entire app**.

- `Gmarket Sans` — all UI, body, buttons, recommendation cards, AI chat, headings
- `Noto Sans KR` — fallback only

The webfont is loaded from:

`https://cdn.jsdelivr.net/gh/fonts-archive/GmarketSans/GmarketSans.css`

No font binary files are bundled in this project.
