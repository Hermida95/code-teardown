---
name: code-teardown
description: Static teardown of an existing artifact (a source repo, Python .pyc files or compiled packages, or a Docker image) that explains how it is built and gives a context-weighted critical verdict - what is good, what is bad, what depends on how it is used - with cited evidence. Use whenever the user wants to study, understand, review or critique someone else's code, a .pyc, a wheel, or a Docker image, even if they never say "teardown". Not a malware or security-research tool.
compatibility: Requires Python 3.11+. Docker CLI only for analyzing images by name. Optional - pycdc or decompyle3 for better .pyc output.
license: MIT
---

# code-teardown

Work in progress. See the plan in README.md.
