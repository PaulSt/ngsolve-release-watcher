# NGSolve release watcher

This github app checks PyPI daily for new stable NGSolve versions.
Repositories with the app installed will receive a workflow dispatch event if a new NGSolve version is found.
The workflow can then build and publish a release candidate (rc) automatically.
If an NGSolve-dependent package is missing a compatible release, the watcher waits until tomorrow without triggering the add-on's workflow.

## Install on an add-on

1. [Install the GitHub App](https://github.com/apps/ngsolve-release-watcher/installations/new) on your repository. 
   Your package needs a root `pyproject.toml` and setuptools-scm versioning.
2. Add these inputs to `.github/workflows/build.yml` on your default branch:

   ```yaml
   on:
     workflow_dispatch:
       inputs:
         ngsolve_version:
           type: string
         dry_run:
           type: boolean
           default: true
   ```

3. In each build job, add this step after checkout and before building:

   ```yaml
   - uses: PaulSt/ngsolve-release-watcher@main
     id: release
     with:
       ngsolve_version: ${{ inputs.ngsolve_version }}
       dry_run: ${{ inputs.dry_run }}
   ```

The action pins NGSolve and compatible add-ons and sets the RC version. 
See it work in e.g. [ngstrefftz](https://github.com/PaulSt/NGSTrefftz/blob/main/.github/workflows/build.yml)
