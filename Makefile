SHELL := /bin/bash

# test
.PHONY: test-sitl
test-sitl:
	@python test/sitl/sitl_test_mock_gps_node.py

.PHONY: test-launch
test-launch:
	@launch_test test/launch/test_px4_launch.py
	@launch_test test/launch/test_ardupilot_launch.py
# test end

.PHONY: test-static
test-static:
	@pre-commit run --all-files
# test end

.PHONY: docs
docs:
	@$(MAKE) -C docs html
	@cd docs/_build/html && touch .nojekyll  # for GitHub Pages
