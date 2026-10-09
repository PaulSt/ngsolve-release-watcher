"""Notify add-ons and build release candidates without changing Git history."""

import argparse
import json
import os
import re
import subprocess
import tempfile
import tomllib
from fnmatch import fnmatch
from functools import cache
from graphlib import TopologicalSorter
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

NGSOLVE = r"\d+\.\d+\.\d{4}(?:\.post\d+)?"


@cache
def projects():
    return tomllib.loads(Path(__file__).with_name("projects.toml").read_text())["projects"]


def stable(releases):
    return sorted((v for v, files in releases.items() if re.fullmatch(NGSOLVE, v)
                   and any(not f.get("yanked", False) for f in files)), key=Version)


@cache
def release_versions(version):
    registry = projects()
    order = TopologicalSorter({name: p.get("requires", []) for name, p in registry.items()})
    released = {}
    for name in order.static_order():
        dependencies = {d: released[d] for d in registry[name].get("requires", [])}
        released[name] = (matching(name, version, dependencies, registry[name].get("wheels", []))
                          if all(dependencies.values()) else None)
    return released


def watch(args):
    """Dispatch the add-on's build workflow using the App's Actions permission."""
    from github import Auth, GithubIntegration

    versions = stable(pypi("ngsolve")["releases"])
    version = next(v for v in reversed(versions) if not args.version or v == args.version)
    registry = projects()
    released = release_versions(version)
    targets = {p["repository"].lower(): name for name, p in registry.items()}
    failed, found = False, False
    auth = Auth.AppAuth(os.environ["APP_CLIENT_ID"], os.environ["APP_PRIVATE_KEY"])
    with GithubIntegration(auth=auth, per_page=100) as app:
        for installation in app.get_installations():
            if installation.suspended_at:
                continue
            for repo in installation.get_repos():
                if repo.archived or repo.disabled:
                    continue
                if args.repository and repo.full_name.lower() != args.repository.lower():
                    continue
                if repo.full_name.lower() not in targets:
                    continue
                found = True
                try:
                    name = targets[repo.full_name.lower()]
                    project = registry[name]
                    if released[name] and not args.dry_run:
                        print(f"{name}: already published ({released[name]})")
                        continue
                    dependencies = {d: released[d] for d in project.get("requires", [])}
                    missing = [d for d, v in dependencies.items() if not v]
                    if missing:
                        print(f"{name}: waiting for {', '.join(missing)} on NGSolve {version}")
                        continue
                    workflow = repo.get_workflow(args.workflow or project.get("workflow", "build.yml"))
                    if any(workflow.get_runs(status=s).totalCount for s in
                           ("queued", "in_progress", "waiting", "pending", "requested")):
                        print(f"{name}: build already active")
                        continue
                    inputs = {"ngsolve_version": version, "dry_run": str(args.dry_run).lower()}
                    if dependencies:
                        inputs["dependencies"] = json.dumps(dependencies)
                    workflow.create_dispatch(repo.default_branch, inputs, throw=True)
                    print(f"{repo.full_name}: NGSolve {version}, dry_run={args.dry_run}")
                except Exception as error:
                    print(f"::error::{repo.full_name}: {error}")
                    failed = True
    if args.repository and not found:
        print(f"::error::{args.repository} is not registered or accessible to the App")
        failed = True
    return int(failed)


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def pin(text, versions):
    """Replace declared build/runtime requirements, preserving markers and formatting."""
    data = tomllib.loads(text)
    for value in data["project"].get("dependencies", []) + data["build-system"]["requires"]:
        requirement = Requirement(value)
        name = canonicalize_name(requirement.name)
        if name in versions:
            extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
            prefix = re.escape(value.split(";")[0].strip())
            text = re.sub(r'''(["'])''' + prefix + r'''(?=\s*[;"'])''',
                          lambda m: f"{m[1]}{requirement.name}{extras}=={versions[name]}", text)
    return text


@cache
def pypi(name, version=None):
    route = quote(name, safe="") + (f"/{quote(version, safe='')}" if version else "")
    try:
        with urlopen(f"https://pypi.org/pypi/{route}/json", timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code != 404 or version is not None:
            raise
        error.close()
        return {"releases": {}}


def matching(name, ngsolve_version, dependencies=None, wheels=()):
    """Find a published wheel release with the exact NGSolve pin and required files."""
    releases = pypi(name)["releases"]
    for value in sorted(releases, key=Version, reverse=True):
        if Version(value).micro != Version(ngsolve_version).micro:
            continue
        files = [f["filename"] for f in releases[value]
                 if f["packagetype"] == "bdist_wheel" and not f.get("yanked", False)]
        if not files or not all(any(fnmatch(f, pattern) for f in files) for pattern in wheels):
            continue
        requirements = [Requirement(r) for r in pypi(name, value)["info"]["requires_dist"] or []]
        if (any(canonicalize_name(r.name) == "ngsolve" and not r.marker
                and str(r.specifier) == f"=={ngsolve_version}" for r in requirements)
                and all(r.specifier.contains(dependencies[canonicalize_name(r.name)], prereleases=True)
                        for r in requirements if canonicalize_name(r.name) in (dependencies or {}))):
            return value


def candidate(name, ngsolve_version, dependencies):
    """Use PyPI to reuse a matching release or select the next RC."""
    versions = sorted(map(Version, pypi(name)["releases"]))
    latest = max(versions or [Version(t[1:]) for t in git("tag", "--list", "v*").splitlines()]
                 or [Version("0.1.0")])
    base = Version(f"{latest.major}.{latest.minor}.{Version(ngsolve_version).micro}")
    series = [v for v in versions if v.release == base.release]
    existing = matching(name, ngsolve_version, dependencies)
    if existing:
        complete = matching(name, ngsolve_version, dependencies,
                            projects().get(canonicalize_name(name), {}).get("wheels", []))
        return complete or existing, bool(complete)
    assert all(v.pre for v in series), f"{base} is already finalized on PyPI"
    number = 1 + max((v.pre[1] for v in series), default=0)
    return f"{base}rc{number}", False


def configure_build(version, dry_run=True, dependencies=None):
    """Change only the build checkout's pins and record the selected versions."""
    path = Path("pyproject.toml")
    text = path.read_text()
    name = tomllib.loads(text)["project"]["name"]
    dependencies = dependencies if dependencies is not None else {
        d: release_versions(version)[d]
        for d in projects().get(canonicalize_name(name), {}).get("requires", [])}
    assert all(dependencies.values()), f"Waiting for release prerequisites: {dependencies}"
    selected, published = candidate(name, version, dependencies)
    path.write_text(pin(text, {"ngsolve": version, **dependencies}))
    variable = "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_" + re.sub(r"[-_.]+", "_", name).upper()
    forwarded = os.getenv("CIBW_ENVIRONMENT_PASS_LINUX", "").split()
    with open(os.environ["GITHUB_ENV"], "a") as output:
        output.write(f"{variable}={selected}\n")
        output.write(f"CIBW_ENVIRONMENT_PASS_LINUX={' '.join(dict.fromkeys([*forwarded, variable]))}\n")
    record = json.dumps({
        "source_commit": git("rev-parse", "HEAD"),
        "ngsolve_version": version,
        "package_name": name,
        "package_version": selected,
        "dependencies": dependencies,
        "dry_run": dry_run,
        "already_published": published,
    }, indent=2) + "\n"
    print(record)
    with tempfile.NamedTemporaryFile(mode="w", prefix="ngsolve-release-", suffix=".json",
                                     dir=os.getenv("RUNNER_TEMP"), delete=False) as output:
        output.write(record)
        record_path = Path(output.name)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
            output.write(f"### NGSolve release build\n\n```json\n{record}```\n")
    return {"version": selected, "published": str(published).lower(),
            "record_path": str(record_path), "record_name": record_path.stem}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["watch", "build"])
    parser.add_argument("--version", default=os.getenv("NGSOLVE_VERSION") or None)
    parser.add_argument("--repository", default=os.getenv("TARGET_REPOSITORY") or None)
    parser.add_argument("--workflow", default=os.getenv("TARGET_WORKFLOW") or None)
    parser.add_argument("--dependencies", default=os.getenv("RELEASE_DEPENDENCIES") or "null")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction,
                        default=os.getenv("DRY_RUN", "true").strip().lower() != "false")
    args = parser.parse_args()
    if args.command == "watch":
        return watch(args)
    result = configure_build(args.version, args.dry_run, json.loads(args.dependencies))
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.writelines(f"{key}={value}\n" for key, value in result.items())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
