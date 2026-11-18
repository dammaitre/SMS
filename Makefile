# `make` builds a standalone ./btsms executable (no venv needed to run it).
VENV   := .venv
PY     := $(VENV)/bin/python
BUILD  := build

.PHONY: all test clean distclean

all: btsms

$(PY):
	python3 -m venv $(VENV)
	$(PY) -m pip install -q -e '.[build]'

btsms: $(PY) packaging/btsms.spec packaging/entry.py pyproject.toml $(wildcard src/btsms/*.py)
	$(PY) -m PyInstaller --noconfirm --clean --log-level WARN \
		--distpath $(BUILD)/dist --workpath $(BUILD)/work packaging/btsms.spec
	mv $(BUILD)/dist/btsms ./btsms

test: $(PY)
	$(PY) -m unittest discover -s tests

clean:
	rm -rf $(BUILD) btsms src/*.egg-info
	find . -name __pycache__ -not -path './$(VENV)/*' -prune -exec rm -rf {} +

distclean: clean
	rm -rf $(VENV)
