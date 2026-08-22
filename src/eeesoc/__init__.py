"""eeesoc — soccer Matches + Similar lookalike dashboard."""

__all__ = ["main"]
__version__ = "0.1.0"


def main(argv: list[str] | None = None) -> None:
    from eeesoc.cli import main as _main

    _main(argv)
