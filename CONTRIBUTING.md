# Contributing to hot-repos

Thank you for your interest in contributing to `hot-repos`! We welcome bug reports, feature requests, and pull requests.

## Reporting Bugs

If you find a bug, please open an issue on GitHub. To help us resolve it quickly, please include:
- A clear and concise description of the bug.
- Steps to reproduce the issue.
- Expected vs. actual behavior.
- Your operating system and Python version.

## Proposing Features

We are always open to new ideas! If you have a feature request:
- Open an issue on GitHub.
- Explain why the feature would be useful.
- Provide examples of how you imagine it working.

## Submitting Pull Requests

1. **Fork** the repository.
2. **Create a branch** for your changes (e.g., `fix/bug-description` or `feat/feature-name`).
3. **Implement your changes**, ensuring you follow the project's coding conventions.
4. **Run the tests** to make sure everything still works.
5. **Commit your changes** with clear, descriptive commit messages.
6. **Push** to your fork and **open a pull request** against the `main` branch.

## Coding Conventions

- **Language**: Python 3.11+
- **Style**: We follow [PEP 8](https://peps.python.org/pep-0008/) for coding style. Please keep your code clean, readable, and well-documented where necessary.
- **Dependencies**: Add any new dependencies to `pyproject.toml`.

## Running Tests

To run the tests, ensure you have your environment set up and then run:

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

If you don't have a virtual environment set up:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -t .
```
