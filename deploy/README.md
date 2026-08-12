# Deployment

## What is actually deploying this site

GitHub Pages is configured to build from **branch `main`, folder `/docs`**. The published
site is therefore just the `docs/` directory in this repo — `docs/index.html` plus the run
artifacts in `docs/data/`. A `docs/.nojekyll` file disables Jekyll so the page is served
exactly as committed.

To republish after a new experiment run:

```bash
cp artifacts/*.json docs/data/    # keep the published copy in sync with the artifacts
git add -A && git commit -m "Republish site" && git push origin main
```

`artifacts/` is the single source of truth; `docs/data/` is a published copy of it. That
copy is the one manual step, and it is the thing to check first if the site and the
artifacts ever disagree.

## Things that did not work, and why

Worth recording, because both cost real time:

- **GitHub Actions is not available here.** The credential in this environment lacks the
  `workflow` OAuth scope, so any push whose diff touches `.github/workflows/**` is rejected
  outright. `github-pages-workflow.yml` in this directory is the workflow I would otherwise
  use: it runs `actions/configure-pages` with `enablement: true`, copies `artifacts/*.json`
  into the site at build time so the two can never drift, and deploys with
  `actions/deploy-pages`. To adopt it, move it to `.github/workflows/deploy.yml` and push
  with a token that has the `workflow` scope.
- **The Pages REST API is unreachable** through this environment's proxy
  (`GET /repos/{owner}/{repo}/pages` returns 403), so the Pages source cannot be changed
  programmatically from here.
- **A `gh-pages` branch is not what serves this site.** I published one early on, before
  discovering the Pages source was set to `main` + `/docs`. Builds from that branch stopped
  being deployed, so the live site silently served a stale copy while the branch looked
  correctly updated. The lesson is that a green push tells you nothing about what Pages is
  actually building — check the `pages build and deployment` run, not just the branch
  contents.
