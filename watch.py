"""Notify add-ons and build release candidates without changing Git history."""

import argparse
import json
import os
import re
import subprocess
import tempfile
import tomllib
from functools import cache
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

NGSOLVE = r"\d+\.\d+\.\d{4}(?:\.post\d+)?"


def stable(releases):
    return sorted((v for v, files in releases.items() if re.fullmatch(NGSOLVE, v)
                   and any(not f.get("yanked", False) for f in files)), key=Version)


def ngsolve_pin(info):
    for value in info.get("requires_dist") or []:
        requirement = Requirement(value)
        if canonicalize_name(requirement.name) == "ngsolve" and not requirement.marker:
            pin = re.fullmatch(r"==([^,*]+)", str(requirement.specifier))
            if pin:
                return pin[1]


@cache
def matching(name, version):
    """Newest published release with this exact NGSolve pin, including RCs."""
    releases = pypi(name)["releases"]
    for value in sorted(releases, key=Version, reverse=True):
        if any(not f.get("yanked", False) for f in releases[value]):
            pin = ngsolve_pin(pypi(name, value)["info"])
            if pin == version:
                return value
            # Assume NGSolve pins increase with package versions.
            if pin and Version(pin) < Version(version):
                return None


def dependencies(project, version):
    requirements = project["project"].get("dependencies", []) + project["build-system"]["requires"]
    names = {canonicalize_name(Requirement(r).name) for r in requirements} - {"ngsolve"}
    selected = {}
    for name in sorted(names):
        releases = pypi(name)["releases"]
        if releases and ngsolve_pin(pypi(name, max(releases, key=Version))["info"]):
            selected[name] = matching(name, version)
    return selected


def watch(args):
    """Dispatch the add-on's build workflow using the App's Actions permission."""
    from github import Auth, GithubIntegration

    versions = stable(pypi("ngsolve")["releases"])
    version = next(v for v in reversed(versions) if not args.version or v == args.version)
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
                found = True
                try:
                    project = tomllib.loads(repo.get_contents("pyproject.toml").decoded_content.decode())
                    if not args.dry_run and matching(project["project"]["name"], version):
                        continue
                    selected = dependencies(project, version)
                    missing = [name for name, value in selected.items() if not value]
                    if missing:
                        print(f"{repo.full_name}: waiting for {', '.join(missing)} on NGSolve {version}")
                        continue
                    # A checked request preserves GitHub's error message.
                    repo.requester.requestJsonAndCheck(
                        "POST", f"{repo.url}/actions/workflows/{quote(args.workflow, safe='')}/dispatches",
                        input={"ref": repo.default_branch, "inputs": {
                            "ngsolve_version": version, "dry_run": str(args.dry_run).lower(),
                        }},
                    )
                    print(f"{repo.full_name}: NGSolve {version}, dry_run={args.dry_run}")
                except Exception as error:
                    print(f"::error::{repo.full_name}: {error}")
                    failed = True
    if args.repository and not found:
        print(f"::error::{args.repository} is not accessible to the App")
        failed = True
    return int(failed)


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def pin(text, versions):
    project = tomllib.loads(text)
    for value in project["project"].get("dependencies", []) + project["build-system"]["requires"]:
        requirement = Requirement(value)
        name = canonicalize_name(requirement.name)
        if name in versions:
            extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
            _, separator, marker = value.partition(";")
            replacement = f"{requirement.name}{extras}=={versions[name]}{separator}{marker}"
            for quote in ('"', "'"):
                text = text.replace(f"{quote}{value}{quote}", f"{quote}{replacement}{quote}")
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


def candidate(name, ngsolve_version):
    """Use PyPI to reuse a matching release or select the next RC."""
    versions = sorted(map(Version, pypi(name)["releases"]))
    latest = max(versions or [Version(t[1:]) for t in git("tag", "--list", "v*").splitlines()]
                 or [Version("0.1.0")])
    base = Version(f"{latest.major}.{latest.minor}.{Version(ngsolve_version).micro}")
    series = [v for v in versions if v.release == base.release]
    for value in reversed(series):
        requirements = map(Requirement, pypi(name, str(value))["info"]["requires_dist"])
        if any(r.name.lower() == "ngsolve" and str(r.specifier) == f"=={ngsolve_version}"
               for r in requirements):
            return str(value), True
    assert all(v.pre for v in series), f"{base} is already finalized on PyPI"
    number = 1 + max((v.pre[1] for v in series), default=0)
    return f"{base}rc{number}", False


def configure_build(version, dry_run=True):
    """Change only the build checkout's pins and record the selected versions."""
    path = Path("pyproject.toml")
    text = path.read_text()
    project = tomllib.loads(text)
    name = project["project"]["name"]
    selected_dependencies = dependencies(project, version)
    assert all(selected_dependencies.values()), f"Waiting for release prerequisites: {selected_dependencies}"
    selected, published = candidate(name, version)
    path.write_text(pin(text, {"ngsolve": version, **selected_dependencies}))
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
    parser.add_argument("--workflow", default=os.getenv("TARGET_WORKFLOW") or "build.yml")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction,
                        default=os.getenv("DRY_RUN", "true").strip().lower() != "false")
    args = parser.parse_args()
    if args.command == "watch":
        return watch(args)
    result = configure_build(args.version, args.dry_run)
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.writelines(f"{key}={value}\n" for key, value in result.items())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
