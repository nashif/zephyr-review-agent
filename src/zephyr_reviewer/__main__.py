# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0
"""`python -m zephyr_reviewer`: the command line without an installed entry point, as the action runs it."""

import sys

from .cli import main

sys.exit(main())
