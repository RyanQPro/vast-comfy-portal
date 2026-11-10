# Notice

`portal-aio/` is the Vast.ai Instance Portal from
[vast-ai/base-image](https://github.com/vast-ai/base-image) (portal v3.1.8),
distributed under the Vast.ai Elastic License (see [LICENSE.md](LICENSE.md)), for use
with Vast.ai.

**This copy has been modified.** Changes from the upstream portal:

| File | Change |
|---|---|
| `portal-aio/portal/model_manager.py` | Added: on-demand model downloads and catalog setups (`/models/*` routes) |
| `portal-aio/portal/static/assets/models.js` | Added: the Models page |
| `portal-aio/portal/static/assets/models.css` | Added: styles for the Models page |
| `portal-aio/tests/test_model_manager.py` | Added: tests for the above |
| `portal-aio/portal/portal.py` | Modified: registers the `/models` router and stops downloads on shutdown |
| `portal-aio/portal/templates/index.html` | Modified: Models menu entry, page skeleton, script and stylesheet includes |
| `portal-aio/portal/static/assets/portal.js` | Modified: page title and refresh when the Models page opens |
| `portal-aio/VERSION` | Modified: `v3.1.8-models.2` |

All other files are unchanged from upstream.
