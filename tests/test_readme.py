from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"
COMMON = "### Common requests"
REFERENCE = "### Reference"


def _common_section(text: str) -> str:
    start = text.index(COMMON)
    end = text.index(REFERENCE, start)
    return text[start:end]


def test_common_requests_leads_the_excludes_section():
    text = README.read_text()
    excludes = text.index("## Inspecting & Applying Excludes")
    assert text.index(COMMON, excludes) < text.index(REFERENCE, excludes)


def test_each_recipe_names_its_command_and_safety_line():
    section = _common_section(README.read_text())
    recipes = {
        "Remove a directory from the backups already in the repo": [
            "apply excludes --job data",
            "--now",
            "--later",
            "never touches\nlive files",
            "no undo",
        ],
        "Try a pattern before you deploy it": [
            "show excludes --live -e",
            "--excludes-file",
            "never modify the config",
        ],
        "See what a candidate rule would newly exclude": [
            "--reveal",
            "--reveal-file",
            "never handed to restic",
        ],
        "Survey what the current rules match": [
            "show excludes",
            "--all",
            "--price",
            "read only",
        ],
    }
    for heading, needles in recipes.items():
        start = section.index(f"#### {heading}")
        end = section.find("#### ", start + 1)
        body = section[start : end if end != -1 else None]
        for needle in needles:
            assert needle in body, (heading, needle)
