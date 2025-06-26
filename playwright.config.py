# playwright.config.py

# This configuration enables Playwright for pytest in headless mode
# and sets a default viewport for all tests.


import pytest

from playwright.sync_api import sync_playwright

def pytest_configure(config):
    # Enable headless mode
    config.option.headless = True
    # Set a consistent viewport size for all tests
    config.option.viewport = {'width': 1280, 'height': 720}
