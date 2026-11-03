import pathlib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

import click
import typer
from rich.console import Console

app = typer.Typer(
    name="chronotrace",
    help="AI-Powered Temporal Code Intelligence",
)

# Global to hold --config value for subcommands (Typer/Click context isolation)
_CONFIG_PATH: pathlib.Path | None = None


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    config: pathlib.Path | None = typer.Option(  # noqa: B008
        None, "--config", help="Path to chronotrace.yml", show_default=False
    ),
):
    """ChronoTrace - AI-Powered Temporal Code Intelligence."""
    global _CONFIG_PATH
    _CONFIG_PATH = config
    ctx.obj = {"config": config}


def _get_version() -> str:
    try:
        return _pkg_version("chronotrace")
    except PackageNotFoundError:
        from chronotrace import __version__

        return __version__


@app.command()
def version():
    """Print the installed ChronoTrace version."""
    # Validate --config if provided (global path)
    cfg_path = _CONFIG_PATH
    if cfg_path is None:
        # Fallback to Click context (for direct invocation)
        try:
            ctx = click.get_current_context(silent=True)
            if ctx is not None and ctx.obj is not None and isinstance(ctx.obj, dict):
                cfg_path = ctx.obj.get("config")
            # Also check parent context
            if cfg_path is None and ctx is not None and ctx.parent is not None:
                parent_obj = getattr(ctx.parent, "obj", None)
                if isinstance(parent_obj, dict):
                    cfg_path = parent_obj.get("config")
                elif parent_obj is not None:
                    # Typer may store in different place; also check parent params
                    cfg_path = getattr(ctx.parent, "params", {}).get("config")
        except Exception:
            cfg_path = _CONFIG_PATH
    if cfg_path is not None:
        try:
            from chronotrace.config import load_config

            load_config(config_path=cfg_path)
        except click.exceptions.Exit:
            raise
        except Exception as e:
            console = Console(stderr=True)
            console.print(f"[red]Configuration error:[/red] {e}")
            raise typer.Exit(code=1) from e
    typer.echo(_get_version())


if __name__ == "__main__":
    app()
