# python-mwm: MWM protocol library and rig research tools.

PYTHON := python3

.PHONY: build test samples clean

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