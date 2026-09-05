# Source assets

The original brand assets, kept at full resolution.

Nothing imports from here. The web build serves derived, size-appropriate
copies from `frontend/public/brand/`:

| Derived | From | Why |
|---|---|---|
| `brand/accord-hero.jpg` (1600w, 141KB) | `bgimage.png` (1.4MB) | the hero background; the PNG was ten times the size for no visible gain |
| `brand/accord-logo-512.png` | `logo.png` (1312x1199) | nav and footer mark |
| `brand/accord-icon-512.png`, `favicon.svg`, `favicon-64.png`, `apple-touch-icon.png` | redrawn | the full logo's curves dissolve below ~48px, so the tab mark is drawn for the size it is used at |

Regenerate the derived copies from these if the brand ever changes.
