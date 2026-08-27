# 1MS041 — Introduction to Data Science (2026)

This repository publishes the course website and a browser-based JupyterLite environment.

## Published notebook contract

Notebook publication is manifest-driven. The private course release workflow
generates and validates a staged payload, then synchronizes both the approved
student artifacts and `notebooks-manifest.json` into this repository. Do not
publish a notebook by copying it into `notebooks/` by hand.

The manifest has schema version 1 and an ordered `notebooks` array. Every entry
contains:

- `filename`: a safe `.ipynb` basename;
- `section`: `introductory`, `lecture`, `probss`, or `assignment`;
- `order`: a positive integer, unique within the section;
- `assignment_release_state`: `released` or `unreleased` for assignments and
  `null` for every other section; and
- an optional `label` used as the list marker on the course index.

Schema-version-1 documents and entries reject unsupported or duplicate JSON
fields. The manifest must be inside the public root, and notebook filenames
must also be unique when compared without case so the same payload behaves
consistently on case-sensitive and case-insensitive filesystems.

Section and `order`, rather than directory or filename ordering, determine the
course index. An unreleased assignment must be a one-cell placeholder. A
released assignment must contain problem cells, may contain local `Test` cells,
and must not contain solution or hidden uppercase `TEST` cells. Public
notebooks must have no stored outputs or execution counts. Matching HTML
exports are inspected independently for private assignment markers, and
credential-like notebook metadata keys are rejected.

`notebooks/` remains the root of JupyterLite's file browser, so paths such as
`data/example.csv` and `images/plot.png` remain relative to each notebook. The
manifest controls notebook membership; `data/`, `images/`, `Utils.py`, and the
environment files are explicit supporting assets.

The course's pinned Python packages are copied from the 2025 repository into `notebooks/requirements.txt`. For a regular local Jupyter environment, install them with:

```bash
python -m pip install -r notebooks/requirements.txt
```

When matching HTML exports are present, preview the generated Markdown pages,
index, and orphan cleanup from the repository root:

```bash
python3 generate_mds.py --dry-run
```

Generate the pages only after reviewing that preview, then verify that no
managed output is stale:

```bash
python3 generate_mds.py
python3 generate_mds.py --check
```

`--check` performs no writes. It fails when the manifest is invalid, an
approved notebook or matching HTML export is missing, an assignment release
state disagrees with its notebook, private solution/hidden-test content is
present, an unapproved top-level notebook or HTML export remains, or a
generated Markdown page is stale. `--dry-run` previews those generated-file
changes and removals without applying them. Orphan deletion is limited to
top-level `notebooks/*.ipynb`, `html/*.html`, legacy `notebooks/*.html`, and
Markdown files carrying the generated notebook-page signature; hand-authored
Markdown is preserved. JupyterLite consumes `notebooks/`, while rendered
exports live separately in `html/` and therefore do not appear in its file
browser.
The public payload may not contain symlinks, nested notebook exports, or files
named as private assignment variants. Normal writes are first staged on the
same filesystem; existing generated pages and orphans are backed up and
restored if an update fails partway through. Reserved site-page names and
collisions with hand-authored Markdown are rejected instead of overwritten.

The command is independent of the current working directory. A release
orchestrator may validate a staged payload explicitly:

```bash
python3 generate_mds.py \
  --root /path/to/staged-public-payload \
  --manifest notebooks-manifest.json \
  --check
```

Each generated page links to the approved notebook download, its matching
static HTML rendering, and the same manifest-approved path in JupyterLite.

## Run JupyterLite locally

Create a virtual environment and install the build dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-jupyterlite.txt
```

Building Cockle's WebAssembly commands also requires Node.js, npm, and micromamba on `PATH`. On macOS with Homebrew, install them with `brew install node micromamba`. The deployment workflow installs micromamba automatically.

Validate the manifest, build the site, then start the local JupyterLite server:

```bash
python generate_mds.py --check
jupyter lite build
jupyter lite serve
```

Open the URL printed by the command. Notebooks execute in the browser with the Pyodide Python kernel.

The browser environment starts with the same package set as the 2025 course. The original pins in `notebooks/requirements.txt` remain the source for regular local Python environments. Pyodide supplies WebAssembly-compatible builds of IPython, Matplotlib, NumPy, pandas, scikit-learn, SciPy, and SymPy. The additional packages in `requirements-pyodide.txt` are merged into a custom browser lock during the JupyterLite build. The complete course stack is prefetched when the kernel starts, allowing packages imported indirectly by helper modules to work without notebook-level `%pip` setup. The first kernel startup therefore downloads more data, which the browser caches for subsequent use.

The launcher and **File → New** menu also provide a terminal powered by Cockle, a Bash-like shell running in WebAssembly. It supports common filesystem commands, pipelines, and redirection, but it is not a complete Bash environment and does not enable IPython `%%sh` cells.

## Deploy

The GitHub Actions workflow runs the manifest unit tests and
`generate_mds.py --check` before building. The check requires the set of
top-level `.ipynb` files in `notebooks/` to equal the manifest-approved set, so
JupyterLite cannot build with an undeclared notebook. The workflow then builds
JupyterLite from `notebooks/`, places it under `/lite/`, builds the Jekyll
course site, and deploys both as one GitHub Pages artifact. In the GitHub
repository settings, set **Pages → Build and deployment → Source** to **GitHub
Actions**.

The deployed JupyterLite application will be available at:

```text
https://datascience-intro.github.io/1MS041-2026/lite/lab/index.html
```

Browser edits are stored locally in IndexedDB. A locally edited notebook can take precedence over a newer published copy until its local copy is removed.
