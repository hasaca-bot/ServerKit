# Marketing site

Plain static HTML, CSS and a little vanilla JS. No build step, no frameworks, no CDNs.
"Deployra" and `example.com` are placeholders (see `docs/HOSTING-ROADMAP.md`).

```
landing/
  index.html, pricing.html, legal/*.html   pages
  css/tokens.css                           design tokens (copied from the panel) + fonts
  css/base.css                             reset, typography, shared components
  css/sections.css                         page sections
  src/config.js                            brand, domain, URLs, emails  <- edit this
  src/pricing.js                           all prices and plan features <- edit this
  src/main.js                              config injection, theme toggle, pricing render
  src/theme-init.js                        applies the saved theme before first paint
  fonts/                                   IBM Plex (OFL; licence included)
```

## Change the brand, domain and URLs

Edit `src/config.js` only. Any element with `data-cfg="brand"` (text) or
`data-cfg-href="signupUrl"` (link target; `*Email` keys become `mailto:`) is filled in at
runtime. The HTML keeps the placeholder text as a no-JS fallback, so for a final launch also
search and replace `Deployra` / `example.com` in the `.html` files (titles, meta and OG tags are
static for crawlers).

## Change prices

Edit `src/pricing.js` (USD, excluding VAT). Plans, compute, databases, VPS and the feature
matrix render from it; yearly price is `monthly x (12 - yearlyMonthsFree)`. The pricing tables
and plan cards in the HTML contain a pre-rendered copy of the same numbers for visitors
without JS: after changing prices, update those blocks too (open the page in a browser, copy
the rendered markup of `#index-plans`, `#pricing-plans`, `#compute-table`, `#db-table`,
`#vps-table`, `#matrix`).

## Preview

```bash
cd landing && python3 -m http.server 8080
```

## Deploy as a static site

Upload the `landing/` folder as-is to any static host (nginx root, object storage + CDN, or a
static-site service). Pages use relative links, so it works at a domain root or sub-path. Serve
`fonts/*.woff2` with a long cache lifetime.

## Notes

- Dark by default; follows `prefers-color-scheme`; the header toggle stores `theme` in
  `localStorage` (in try/catch).
- `fonts/ibm-plex-sans-400.woff2` was replaced with the Latin subset from `@fontsource/ibm-plex-sans`
  because the copy in `frontend/public/fonts/` fails to decode in Chromium.
- Legal pages are placeholders marked "under legal review".
- Keep the footer credit to ServerKit (MIT) intact.
