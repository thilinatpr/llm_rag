# DESIGN.md: citizenlk.com visual design

Design rules for every page under citizenlk.com (homepage, Bill Assistant, Procurement Rules, later services).
The owner approved the minimal homepage on 2026-11-06 (`site/index.html`, a standalone static page);
this file records what it uses so other pages match it. Start new pages from that file.
Applied to the live app on 2026-11-06: the chat page `public/index.html` ("Bill Assistant") and the sign-in page
(`LOGIN_PAGE` in `src/auth.js`) use these tokens; keep them in step with this file.

## Principles

1. **Simple, like early Google.** One centred column, one name, one ask box, the services as plain buttons. Nothing else
   on the homepage.
2. **No technology on public pages.** Do not mention models, retrieval, chunk counts, pipelines, grading or typefaces.
   Say what the user gets ("answers cite the official text"), not how.
3. **Only list what exists.** A service is shown only when it is live (link) or being built ("· soon", no link). No idea
   lists, roadmaps or "coming next" sections.
4. **Honesty lines stay.** The footer always says: answers cite the official text, not legal advice, not a government service.
5. **Sri Lankan, not generic.** Colours come from Kandyan temple paintings; typefaces are Sri Lankan (Mooniak). One
   ornament only: the lotus-petal (palapethi) border at the top of the page.

## Colour tokens

Every colour is a CSS variable. Light values on `:root`; dark values in both
`@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {...} }` and `:root[data-theme="dark"] {...}`.

| token | light | dark | meaning / use |
|---|---|---|---|
| `--lime` | `#F4F2EE` | `#15100D` | page background (lime-washed wall) |
| `--ink` | `#1C1511` | `#F1E6D6` | text (lamp-black) |
| `--muted` | `#6E6157` | `#B3A391` | secondary text, placeholder |
| `--line` | `#DAD3CA` | `#3A2D24` | borders, footer rule |
| `--box` | `#FFFFFF` | `#211913` | ask box / input surface |
| `--sindoor` | `#A3241B` | `#E0675A` | accent: the "lk" in the name, petal border, icon |
| `--ochre` | `#E0A93A` | `#E8B550` | hover and focus ring |

Rules: `--sindoor` is the only strong colour on a page, used in at most three places. No gradients, no shadows except the
soft hover shadow on the ask box. Button fill = `color-mix(in srgb, var(--ink) 6%, var(--lime))`.

## Typography

Load from Google Fonts only:
`https://fonts.googleapis.com/css2?family=Abhaya+Libre:wght@700;800&family=Yaldevi:wght@400;500&family=Catamaran:wght@500&display=swap`

| role | face | fallback | use |
|---|---|---|---|
| display | Abhaya Libre 800 | `"Noto Serif Sinhala", Georgia, serif` | the wordmark and page titles only |
| body | Yaldevi 400/500 | `"Noto Sans Sinhala", "Segoe UI", system-ui, sans-serif` | everything else, Sinhala and English |
| Tamil | Catamaran 500 | `"Noto Sans Tamil", sans-serif` | Tamil text (`lang="ta"`) |

Abhaya Libre and Yaldevi cover both Sinhala and Latin, so mixed-language lines match. Mark language spans with `lang="si"` /
`lang="ta"`. Wordmark size `clamp(3.4rem, 11vw, 6.2rem)`; body 1rem / 1.6.

## Components

- **Chat page:** top bar (wordmark, "Bill Assistant", Thorough | Fast switch, New chat, Sign out); one 700px column; the user's
  message is a tinted bubble on the right, the answer is plain text on the page with citations like `[070/2026 §4(b)]` in
  `--sindoor`; the ask box is fixed at the bottom. First screen: heading, one sentence, three example questions.
- **Sign-in:** the home layout: wordmark, tagline, one pill form for the email, then one for the 6-digit code.

- **Wordmark:** `citizen<b>lk</b>` in the display face, "lk" in `--sindoor`. Below it, muted:
  `පුරවැසි · குடிமக்கள் · Citizen`.
- **Ask box:** pill (`border-radius: 999px`), max 560px wide, `--box` background, 1px `--line` border, search icon in
  `--sindoor`; hover: `--ochre` border + soft shadow. Until a service accepts a question in its URL, it is a link to that
  service, not a form.
- **Service buttons:** 6px radius, tinted fill (above), 9px 18px padding. Live = link. Not live = plain text with " · soon".
- **Petal border:** 14px strip at the very top:
  `background: radial-gradient(circle at 50% 0, var(--sindoor) 0 7px, transparent 7.5px) 0 0 / 20px 14px repeat-x;`
- **Footer:** one line, `--muted`, top rule `--line`: place on the left, honesty line on the right.

## Layout

- `html, body { height: 100% }`; body is a flex column: petals, `main` (flex 1, content centred both ways, `gap: 28px`), footer.
- Side gutter `max(16px, env(safe-area-inset-left/right, 0px))`; must work at 400px width with no sideways scroll.
- Focus: `outline: 2px solid var(--ochre); outline-offset: 3px`.

## Copy

Plain, short, user-side words: "Ask about any bill in Parliament", "Bill Assistant", "Procurement Rules".
No marketing adjectives, no exclamation marks, no emoji.

## Avoid

Hero sections, feature grids, statistics, "how it works" steps, testimonials, roadmaps, gradients, stock icons, Inter /
Space Grotesk, purple-blue themes, and any claim about a service that is not live.
