# Deployment

## What is actually deploying this site

The published site is the contents of `site/` pushed to the **root of the `gh-pages`
branch**. GitHub Pages serves that branch automatically. `main` holds the source,
experiments and raw run artifacts; `gh-pages` holds only the built site.

To republish after a new run:

```bash
cp artifacts/*.json site/data/     # keep the site's data in sync with the artifacts

tmp=$(mktemp -d)
cp -r site/. "$tmp"/
touch "$tmp"/.nojekyll
cd "$tmp"
git init && git checkout -b gh-pages
git add -A && git commit -m "Publish static showcase site"
git remote add origin https://github.com/carlosrymer/openai-agents-sdk-v2-durable-sandbox.git
git push -u origin gh-pages --force
```

The `artifacts/` → `site/data/` copy is the one manual step. `artifacts/` on `main` is
the single source of truth; `site/data/` is a published copy of it.

## What I would have preferred, and why it is not here

`github-pages-workflow.yml` in this directory is the GitHub Actions workflow I would
normally use: it runs `actions/configure-pages` with `enablement: true`, copies
`artifacts/*.json` into `site/data/` automatically so the two can never drift, and
deploys with `actions/deploy-pages`.

It is parked here rather than in `.github/workflows/` because the credential available
when I built this lacks the `workflow` OAuth scope, so any push touching
`.github/workflows/**` is rejected outright by GitHub. Configuring Pages through the
REST API was also unavailable. Branch-based publishing was the path that worked.

To adopt the workflow, move this file to `.github/workflows/deploy.yml` and push with a
token that has the `workflow` scope. Pages will then build from Actions instead of the
`gh-pages` branch, and the manual copy step above becomes unnecessary.
