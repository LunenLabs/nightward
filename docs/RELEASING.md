# Releasing nightward

Publishing to PyPI can't be undone: a version number can never be uploaded twice,
and mirrors copy the files the moment they land. Work through this checklist from
top to bottom. Nothing here is automated, on purpose. The release workflow only
runs when a human starts it, and it publishes only after a second human approves
the `pypi` environment.

The steps below use 0.3.0 as the example. For a later release, substitute the
version.

## 0. Hard gate (do not continue without it)

- [ ] Legal review is complete: the owner has cleared the employment and IP
      constraints for publishing nightward under its own name.
      Nothing below happens before this box is checked.

> **History is public.** github.com/LunenLabs/nightward is already public, with a
> squashed history. Everything on `main` and on any pushed branch is published the
> moment it's pushed. Push only branches based on the public history (check
> `git merge-base` against `origin/main`), and never push a branch that was built
> on the old private history.

## 1. Content check (on the release branch, before merging)

- [ ] `live-experiment/` and other personal-context files are not in the tree.
- [ ] No real `.nightward/` store is in the tree. The committed stores under
      `examples/` contain synthetic data only. The only dashboard that is ever
      published is built by `python scripts/build_demo.py`.
- [ ] `nightward.__version__` is the release version (`pyproject.toml` reads it).
- [ ] `CHANGELOG.md` has an entry for the version, still marked "Unreleased".
- [ ] The README quickstart works from the built wheel in a fresh venv:
      `python -m build`, then install `dist/*.whl` into a new venv and run every
      quickstart command in an empty directory outside the repo.
- [ ] `twine check dist/*` passes, and the wheel contains only `nightward/`
      (including `view/assets/index.html`, `app.js` and `style.css`) and
      `nightward-<version>.dist-info/`.

## 2. Merge to main

- [ ] Merge PR #5 (`launch/beta-rounds` -> `main`) after human review.
- [ ] CI is green **on `main`** (the `ci` workflow: the test matrix, the dogfood
      job and the dashboard preview).

## 3. PyPI one-time setup (first release only)

- [ ] Use a pypi.org account with 2FA.
- [ ] The project doesn't exist on PyPI yet, so add a **pending publisher**
      (pypi.org -> Your account -> Publishing -> Add a new pending publisher):
      project `nightward`, owner `LunenLabs`, repository `nightward`, workflow
      `release.yml`, environment `pypi`. This creates no API token; the first
      successful publish turns it into the project's trusted publisher.
- [ ] On GitHub: Settings -> Environments -> `pypi`. Add yourself as a
      **required reviewer**. This is the second human gate on the publish job.

## 4. Tag and publish

- [ ] On `main`, set the CHANGELOG date: `## [0.3.0] - Unreleased` becomes
      `## [0.3.0] - YYYY-MM-DD`. Merge that change the usual way (through a PR).
- [ ] Tag the merged commit and push the tag:
      `git tag -a v0.3.0 -m "nightward 0.3.0"`, then `git push origin v0.3.0`.
- [ ] Actions -> `release` -> Run workflow, and choose **the tag `v0.3.0`**
      under "Use workflow from". The build job checks that the wheel reports
      the tag's version, so a run started from a branch fails before anything
      is published.
- [ ] The workflow runs `gate` (ruff and the full test suite), then `build` (the
      sdist and wheel, plus a smoke install), then waits on the `pypi`
      environment. Check the uploaded `dist` artifact, then approve the
      environment.

## 5. Verify

- [ ] In a clean venv: `pip install nightward`, then `nightward --version`
      prints `nightward 0.3.0`.
- [ ] Run the README quickstart against that install.
- [ ] `pip install "nightward[mcp]"`, then `nightward mcp --help` works.
- [ ] The project page on pypi.org renders the README, and its images and links
      work.
- [ ] Optional: create a GitHub release from the tag, with the CHANGELOG entry
      as its notes.

## 6. Announce (after verification)

- [ ] Remove the "not on PyPI yet" note and the git pre-release install lines
      from the README's Install section, and the git URL in step 1 of the
      CHANGELOG's upgrade guide.
- [ ] Optional: enable the Pages demo workflow (`pages.yml`). It publishes only
      the clean-room synthetic demo.
- [ ] Listings: the pytest plugin list, MCP server directories, awesome-* lists.
- [ ] Launch post.
- [ ] From now on, PRs get human review before merge. The tool's own discipline
      applies to the tool.
