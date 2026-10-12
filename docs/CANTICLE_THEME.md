# Canticle theme

Litigation uses the Canticle Cosmic UI Kit as a shared visual base for Chat,
Projects, Rules, and Knowledge. All assets are served by the local FastAPI app;
the theme has no remote font, script, or stylesheet dependency.

## Vendored sources

| Local asset | Upstream source | Source SHA-256 | Vendored SHA-256 |
| --- | --- | --- | --- |
| `branding/canticle-cosmic.css` | `/home/terrabyte/Documents/Projects/Canticle.cc/branding/canticle-cosmic-kit/css/canticle-cosmic.css` | `2746d0882670382332f0d9de14ddbcf4ffffc10b784e9fdf4f95ee597727a6b2` | `2746d0882670382332f0d9de14ddbcf4ffffc10b784e9fdf4f95ee597727a6b2` |
| `branding/canticle-lockup.svg` | `/home/terrabyte/Documents/Projects/Canticle.cc/branding/kit/marks/canticle-company-lockup.svg` | `c11ecacdafe44241a290e8c9cd06a7c00cd4076ee117207e1762b3dab110c0c2` | `982229ef2bc44ee29d03804e65958bdb28fe83ffb212074c6b48db2e6240ce2a` |

The SVG markup is unchanged; the vendored file has one normalized trailing
newline, which accounts for its different file hash.

`branding/litigation-canticle.css` is the local adaptation layer. It maps the
existing page tokens and controls to the kit palette, typography, focus states,
panels, status colors, and reduced-motion behavior. It loads after each page's
existing stylesheet so current IDs, classes, JavaScript events, and dense data
layouts remain intact.

## Fixed asset routes

- `/branding/canticle-cosmic.css`
- `/branding/litigation-canticle.css`
- `/branding/canticle-lockup.svg`

Each route resolves to one fixed repository file. There is no caller-supplied
path or general static-directory exposure.
