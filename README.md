# 1MS041 — Introduction to Data Science (2026)

This repository publishes the course website and a browser-based JupyterLite environment.

## Add course notebooks

Put notebooks and their supporting files in `notebooks/`. The directory becomes the root of JupyterLite's file browser, so paths such as `data/example.csv` and `images/plot.png` remain relative to each notebook.

The course's pinned Python packages are copied from the 2025 repository into `notebooks/requirements.txt`. For a regular local Jupyter environment, install them with:

```bash
python -m pip install -r notebooks/requirements.txt
```

When matching HTML exports are available, regenerate the course pages from the repository root:

```bash
python3 generate_mds.py
```

Each generated page links to the notebook download, its static HTML rendering, and an executable copy in JupyterLite.

## Run JupyterLite locally

Create a virtual environment and install the build dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-jupyterlite.txt
```

Start the local JupyterLite server:

```bash
jupyter lite serve
```

Open the URL printed by the command. Notebooks execute in the browser with the Pyodide Python kernel.

The browser environment starts with the same package set as the 2025 course. The original pins in `notebooks/requirements.txt` remain the source for regular local Python environments. Pyodide supplies WebAssembly-compatible builds of IPython, Matplotlib, NumPy, pandas, scikit-learn, SciPy, and SymPy. The additional packages in `requirements-pyodide.txt` are merged into a custom browser lock during the JupyterLite build. All of these packages become available automatically when imported; notebook-level `%pip` setup is not required. The first import can still download and cache a package in the browser.

## Deploy

The GitHub Actions workflow builds JupyterLite from `notebooks/`, places it under `/lite/`, builds the Jekyll course site, and deploys both as one GitHub Pages artifact. In the GitHub repository settings, set **Pages → Build and deployment → Source** to **GitHub Actions**.

The deployed JupyterLite application will be available at:

```text
https://datascience-intro.github.io/1MS041-2026/lite/lab/index.html
```

Browser edits are stored locally in IndexedDB. A locally edited notebook can take precedence over a newer published copy until its local copy is removed.
