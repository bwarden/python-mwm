# python-mwm: MWM protocol library and rig research tools.

PYTHON := python3

# Single source of truth for the release version. The `release` target derives
# the tag from this, so a tag can never drift from the version it releases.
VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' python/mwm/__init__.py)

.PHONY: build test samples clean release

build:
	cd python && PYTHONPATH=. $(PYTHON) -m compileall -q mwm tests

test: build
	cd python && PYTHONPATH=. $(PYTHON) -m unittest discover -s tests

# Regenerate the shipped samples/park-*.msh + samples/replay/*.msh from the
# captured park/hat data (deterministic; same flags as the checked-in files).
samples:
	@for src in EMLG000E_filtered.txt EMLG0026_filtered.txt MRDF0007.TXT MRDF0008.TXT; do \
		$(PYTHON) tools/gen_show_script.py --source "$$src" --out-prefix "samples/park-$$src" --split-shows --phase-compress --gap-cap; \
		$(PYTHON) tools/gen_show_script.py --no-trim --source "$$src" > "samples/replay/$$src.msh"; \
	done

clean:
	find python tools -name __pycache__ -type d -exec rm -rf {} +

# Tag and publish a release. Before running: bump __version__ in
# python/mwm/__init__.py, add the matching CHANGELOG.md section, commit both.
# The release itself stays a local, reviewed step -- CI only runs the tests.
release:
	@git diff --quiet HEAD || { echo "refusing to release with uncommitted changes"; exit 1; }
	@test -z "$$(git tag -l v$(VERSION))" || { echo "v$(VERSION) already exists"; exit 1; }
	@grep -q '^## \[$(VERSION)\]' CHANGELOG.md || { echo "CHANGELOG.md has no [$(VERSION)] section"; exit 1; }
	$(MAKE) test
	git tag -a v$(VERSION) -m "mwm $(VERSION)"
	git push origin v$(VERSION)
	gh release create v$(VERSION) --title "mwm $(VERSION)" --generate-notes
