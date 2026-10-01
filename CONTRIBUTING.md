# Contributing to `.dedup`

Thank you for your interest in improving `.dedup`! Every contribution – from bug reports to writing code – is highly appreciated.

## Before you start

1. Read the **[architectural and business documentation](docs/index.md)**. Understanding *why* the tool operates in a certain way is critical (the project has strict rules designed to protect users' data).
2. If you are using AI assistants (e.g., GitHub Copilot, Cursor, Claude, Aider), you must review **[AGENTS.md](AGENTS.md)**, which contains technical guidelines and mandatory rules for code modification.

## Bug Reports (Issues)

If you find a bug, please open an Issue in the repository. Describe:
- What you were trying to do.
- What you expected to happen.
- What actually happened.
- Your Operating System and Python version.
- (If possible) Attach anonymized logs or screenshots, being careful not to publish sensitive file names from your personal archive.

## Proposing Changes (Pull Requests)

1. **Discuss your idea:** Before starting major work (like a new feature or massive refactoring), open an Issue so we can discuss the idea and its alignment with the project's goals.
2. **Safety sandbox (Dry-run):** Remember that all operations by default *cannot* modify data on disk outside of generating manifests and moving files to the internal trash folder `!REMOVE`. Any change affecting the disk will be rigorously tested.
3. **Conventions:** Code must adhere to the rules described in [tests and conventions](docs/technical/development/conventions-and-tests.md). 
   - You are required to write tests without touching the real file system (we use the in-memory `FakeFileSystem` from `tests/fakes.py`).
   - We keep the code extremely clean: complex business decisions and "why it's done this way" are documented inside the `docs/` folder. In the code, we only leave short anchors, e.g., `# See: docs/business/rules/compression.md`.
   - **Language:** documentation (`docs/`), code comments, console output and commit messages are all in English. Only the review UI is localized (English, Polish, Ukrainian) in `tidy/review/locales.py`.

## Creating a Pull Request

1. Fork the repository and create a new branch (`git checkout -b feature/new-feature`).
2. Write tests covering your new feature or fixing the bug.
3. Ensure all tests pass locally:
   ```bash
   python -m pip install -r requirements-dev.txt
   python -m pytest -q
   ```
4. Update the documentation in `docs/` if your change affects business logic, the CLI interface, or the architecture.
5. Commit your changes (Commit messages should be concise and describe the capability you are adding or fixing).
6. Push the branch (`git push origin feature/new-feature`) and open a Pull Request.

Thank you for your time and effort!
