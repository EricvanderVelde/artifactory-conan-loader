# Conan 2 Air-Gapped Package Provisioner

Provisions Conan packages from [conan-center-index](https://github.com/conan-io/conan-center-index) into a self-hosted JFrog Artifactory instance in a fully disconnected environment.

The workflow spans three environments:

```
Disconnected machine          Connected machine        Disconnected machine
(has local CCI clone,         (internet access)        (has Artifactory)
 no internet)
──────────────────────        ─────────────────        ────────────────────
1. fetch.py                                            3. deploy.py
   • reads local CCI             2. download_sources.py    • uploads sources
   • resolves dep tree   ──────►    • curl downloads   ──► • patches recipes
   • copies recipes               • git clone+archive      • conan create
   • generates                                             • conan upload
     download_sources.{json,py}                            • (run tests)
```

---

## Prerequisites

| Tool | Where needed |
|---|---|
| Python 3.10+ with `pyyaml` | disconnected machine |
| `git` | disconnected machine (to read local CCI) |
| `python3`, `curl`, `git` | connected machine (to run download script) |
| Conan 2.1+ | disconnected machine with Artifactory |
| CMake 3.16+, GCC 9+ | disconnected machine (to build packages) |
| Docker + Docker Compose | disconnected machine (to run Artifactory) |

```bash
pip install pyyaml
```

---

## Repository layout

```
packages.yml              # packages to provision (edit this)
profiles/
  linux-x86_64-gcc-cxx17  # default Conan build profile
scripts/
  fetch.py                # Step 1 — resolve deps, copy recipes, generate download script
  deploy.py               # Step 3 — upload to Artifactory and build packages
  provision.py            # shared utility library (not run directly)
  01_setup_artifactory.sh # one-time Artifactory setup
  config.sh               # shared Artifactory config
test_project/             # sample C++ project using protobuf
Dockerfile                # Artifactory OSS container image
docker-compose.yml        # Artifactory + PostgreSQL
```

---

## Step 0 — Start Artifactory (disconnected machine, once)

```bash
docker compose up -d
bash scripts/01_setup_artifactory.sh
```

`01_setup_artifactory.sh` waits for Artifactory to become ready, creates the
`conan-sources` (generic) and `conan-local` (Conan) repositories, and
registers and logs in to the Conan remote.

Default credentials: `admin` / `password` — override via environment variables
or edit `scripts/config.sh`.

---

## Step 1 — Fetch (disconnected machine)

Run `fetch.py` on the disconnected machine, pointing it at your local
conan-center-index clone:

```bash
python3 scripts/fetch.py \
    --packages-file packages.yml \
    --cci-path /path/to/local/conan-center-index \
    --output-dir bundle/
```

If the CCI clone does not exist yet, pass `--cci-url` and the script will
clone it for you (requires git access to that URL):

```bash
python3 scripts/fetch.py \
    --packages-file packages.yml \
    --cci-url https://github.com/conan-io/conan-center-index \
    --output-dir bundle/
```

What the script produces in `bundle/`:

| Path | Contents |
|---|---|
| `recipes/` | Recipe files copied from CCI (original upstream URLs — no Artifactory baked in) |
| `manifest.yml` | Resolved build order: package names, versions, folders, options |
| `download_sources.json` | **Generated data** — every URL/checksum (or git ref) to fetch |
| `download_sources.py` | Generic downloader script (copied verbatim, not generated) — reads the JSON above |

### Dependency resolution

You only need to list the packages you want in `packages.yml`.  All
transitive dependencies are discovered automatically by analysing each
recipe's `conanfile.py`.  Options you set also gate dependency resolution:
setting `with_otlp_grpc: false` means the gRPC stack is not included.

Every package listed at the top level of `packages.yml` is automatically
pinned to the version you gave it, so transitive requirements (e.g. a test
framework's `cmake/[>=3.16]` build requirement) reuse that version instead of
picking the newest one available in CCI.  Listing the same package twice with
two different versions is ambiguous, so it's left unpinned in that case.

```bash
# Pin a transitive dependency that isn't listed directly in packages.yml
python3 scripts/fetch.py --packages-file packages.yml \
    --cci-path ~/cci --pin openssl=3.4.1
```

---

## Step 2 — Download (connected machine)

Transfer `bundle/download_sources.json` and `bundle/download_sources.py`
(same directory) to a machine with internet access and run:

```bash
python3 bundle/download_sources.py
```

The script downloads every source tarball and git-based source archive into
`bundle/sources/` next to the JSON file.  It verifies SHA-256 checksums and is
safe to re-run — already-downloaded files are skipped.

You can pass an explicit destination directory:

```bash
python3 download_sources.py /tmp/conan-sources
```

**Copy `bundle/` back to the disconnected machine** (including the newly
populated `sources/` directory) before proceeding to Step 3.

---

## Step 3 — Deploy (disconnected machine)

```bash
python3 scripts/deploy.py \
    --bundle-dir bundle/ \
    --sources-url http://localhost:8082/artifactory
```

What the script does:

1. Reads `bundle/manifest.yml` for the package list and build order.
2. Uploads every source tarball from `bundle/sources/` to the Artifactory
   generic repository (`conan-sources`).
3. Patches each `conandata.yml` in-place to replace upstream URLs with
   Artifactory URLs.
4. For each package, computes the exact `package_id` for the current
   profile/options (`conan graph info`) and checks whether the Artifactory
   remote already has it (`conan list ... -r <remote>`) — if so, the build is
   skipped. Otherwise runs `conan export`, `conan create`, and `conan upload`
   in dependency order.

Step 4 (the actual Conan package upload) authenticates via the Conan remote
you already configured and logged in to in Step 0 — it needs no credentials
of its own. `--sources-url`/`--sources-user`/`--sources-pass` are only used
for step 2, which is a plain HTTP upload to a *generic* Artifactory repo
(outside Conan's own remote/auth protocol, so `conan remote` can't cover it).

### Options

| Flag | Description |
|---|---|
| `--no-mirror` | Skip source upload (assume sources already in Artifactory) |
| `--no-build` | Skip `conan create` / `conan upload` |
| `--force-build` | Rebuild and re-upload even if the exact `package_id` already exists on the remote |
| `--run-tests` | Build and run `test_project/` after provisioning |
| `--cppstd STD` | C++ standard for `conan create` (default: `17`) |
| `--profile NAME` | Conan profile name or path (default: `linux-x86_64-gcc-cxx17`) |
| `--sources-url URL` | Artifactory base URL for the generic sources-repo upload (default: `http://localhost:8082/artifactory`) |
| `--sources-user USER` | HTTP Basic Auth user for the sources-repo upload (default: `admin`) |
| `--sources-pass PASS` | HTTP Basic Auth password for the sources-repo upload (default: `password`) |
| `--sources-repo REPO` | Artifactory generic repo name for source tarballs (default: `conan-sources`) |

### Environment variables

| Variable | Default | Used by |
|---|---|---|
| `CCI_URL` | `https://github.com/conan-io/conan-center-index` | fetch.py |
| `CCI_PATH` | _(none)_ | fetch.py |
| `ARTIFACTORY_URL` | `http://localhost:8082/artifactory` | deploy.py (default for `--sources-url`) |
| `ARTIFACTORY_USER` | `admin` | deploy.py (default for `--sources-user`) |
| `ARTIFACTORY_PASSWORD` | `password` | deploy.py (default for `--sources-pass`) |
| `CONAN_REMOTE_NAME` | `artifactory` | deploy.py |
| `CONAN_PROFILE` | `linux-x86_64-gcc-cxx17` | deploy.py |

---

## Customising packages.yml

Edit `packages.yml` to choose which packages to provision and which build
options to enable.  Only list the packages you want — dependencies are
resolved automatically.

```yaml
packages:
  - ref: zlib/1.3.2

  - ref: opentelemetry-cpp/1.26.0
    options:
      with_otlp_http: true
      with_otlp_grpc: false
      with_zipkin: false
```

Setting an option to `false` also prevents the dependency it guards from
being included.  Options not listed use the recipe's own `default_options`.

Use `name/version:folder` to pin a specific recipe folder:

```yaml
  - ref: cmake/3.31.12:binary
```

---

## Build profile

The default profile is `profiles/linux-x86_64-gcc-cxx17`:

```ini
[settings]
os=Linux
arch=x86_64
compiler=gcc
compiler.version=15
compiler.libcxx=libstdc++11
compiler.cppstd=17
build_type=Release
```

Copy and edit this file to target a different compiler or architecture, then
pass `--profile your-profile` to `deploy.py`.

---

## Test project

`test_project/` is a small C++ application that uses protobuf.  After
provisioning, verify the packages work end-to-end:

```bash
python3 scripts/deploy.py --bundle-dir bundle/ --run-tests
```

Or run the build script directly:

```bash
bash test_project/build.sh
```

---

## Troubleshooting

**`fatal: repository '...' not found`** during fetch  
The URL passed to `--cci-url` is unreachable.  Check the URL and that git
credentials are configured if the server requires authentication.

**`No sources entry for version X`** during fetch  
The version exists in `config.yml` but has no `conandata.yml` entry.  This
can happen with very new CCI releases.  Use `--pin name=older-version`.

**`ERROR: Source not found in bundle`** during deploy  
A source tarball is missing from `bundle/sources/`.  Re-run
`download_sources.py` on the connected machine and copy the files back.

**Conan exit code 6** (ConanInvalidConfiguration)  
The package is not buildable on this platform (e.g. a Windows-only
dependency resolved on Linux).  Logged as a warning; the rest of the run
continues.
