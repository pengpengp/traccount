"""TAM command-line interface (Typer + Rich)."""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from . import db, machine, vault
from .config import get_trae_data_dir, host_for_region
from .models import Account
from .process_ctl import get_trae_exe_path, set_trae_exe_path
from .switcher import Switcher

app = typer.Typer(
    name="tam",
    help="Trae Account Manager — register, switch, and inspect Trae accounts.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


# ---------------------------------------------------------------------------
@app.command()
def version() -> None:
    """Show version."""
    console.print(f"tam {__version__}")


@app.command(name="list")
def list_accounts(
    only_active: bool = typer.Option(False, "--active", help="only enabled accounts"),
) -> None:
    """List registered accounts."""
    accs = db.list_accounts(only_active=only_active)
    if not accs:
        console.print("[dim]no accounts registered. Run[/dim] tam register")
        return
    current_id = db.get_current_account_id()
    t = Table("current", "id", "email", "name", "region", "plan", "status", "machine_id")
    for a in accs:
        cur = "*" if a.id == current_id else ""
        t.add_row(
            cur, a.id[:8], a.email, a.name, a.region, a.plan_type, a.status,
            (a.machine_id[:8] + "…") if a.machine_id else "",
        )
    console.print(t)


@app.command()
def current() -> None:
    """Show the account currently driving the Trae IDE."""
    cid = db.get_current_account_id()
    if not cid:
        console.print("[dim]no current account[/dim]")
        return
    a = db.get_account(cid)
    if not a:
        console.print(f"[red]stale current account id: {cid}[/red]")
        return
    console.print(f"[bold]{a.email}[/bold]  ({a.name})")
    console.print(f"  region     : {a.region}")
    console.print(f"  plan       : {a.plan_type}")
    console.print(f"  user_id    : {a.user_id}")
    console.print(f"  machine_id : {a.machine_id or '(none)'}")
    console.print(f"  status     : {a.status}")


@app.command()
def switch(
    account_id: str = typer.Argument(..., help="account id (or unique prefix)"),
    launch: bool = typer.Option(True, "--launch/--no-launch", help="launch Trae after switch"),
    reset_registry: bool = typer.Option(False, "--reset-registry", help="reset Windows MachineGuid"),
) -> None:
    """Switch the Trae IDE to the given account."""
    a = _find_account(account_id)
    if a is None:
        console.print(f"[red]no account matching '{account_id}'[/red]")
        raise typer.Exit(1)
    sw = Switcher()
    res = sw.switch_to_account(a, launch=launch, reset_registry=reset_registry)
    console.print(f"[green]switched to[/green] {res['email']}")
    console.print(f"  machine_id : {res['machine_id']}")
    console.print(f"  cleared    : {', '.join(res['cleared']) or '(nothing)'}")
    console.print(f"  registry   : {'reset' if res['registry_reset'] else 'unchanged'}")
    console.print(f"  launched   : {res['launched']}")


@app.command()
def capture(
    name: str = typer.Option("", "--name", help="override account display name"),
    email: str = typer.Option("", "--email", help="override account email"),
) -> None:
    """Import the live Trae IDE session as a new account."""
    sw = Switcher()
    acc = sw.capture_current(name=name, email=email)
    if acc is None:
        console.print("[red]no live Trae session found in storage.json[/red]")
        raise typer.Exit(1)
    console.print(f"[green]captured[/green] {acc.email} (id={acc.id[:8]})")


@app.command()
def clear(
    launch: bool = typer.Option(False, "--launch", help="launch Trae afterwards"),
) -> None:
    """Reset Trae to a fresh-install state (logout)."""
    sw = Switcher()
    res = sw.clear_login_state()
    console.print(f"[green]cleared[/green]  machine_id={res['machine_id']}")
    if launch:
        try:
            sw.ctl.launch()
            console.print("[green]launched[/green] Trae")
        except Exception as e:
            console.print(f"[red]launch failed:[/red] {e}")


@app.command()
def delete(account_id: str = typer.Argument(..., help="account id (or prefix)")) -> None:
    """Delete an account from the local store."""
    a = _find_account(account_id)
    if a is None:
        console.print(f"[red]no account matching '{account_id}'[/red]")
        raise typer.Exit(1)
    if db.delete_account(a.id):
        console.print(f"[green]deleted[/green] {a.email}")
    else:
        console.print(f"[red]delete failed[/red]")


@app.command()
def add(
    email: str = typer.Option(..., "--email", prompt=True),
    token: str = typer.Option(..., "--token", prompt=True, hide_input=True),
    refresh_token: str = typer.Option("", "--refresh-token"),
    user_id: str = typer.Option("", "--user-id"),
    region: str = typer.Option("SG", "--region"),
    name: str = typer.Option("", "--name"),
) -> None:
    """Manually add an account (e.g. importing from a token dump)."""
    acc = Account(
        email=email,
        name=name or email.split("@", 1)[0],
        user_id=user_id,
        region=region,
    )
    secrets = {
        "jwt_token": token,
        "refresh_token": refresh_token,
        "login_info": {
            "token": token, "refresh_token": refresh_token,
            "user_id": user_id, "email": email,
            "username": name, "host": host_for_region(region), "region": region,
        },
    }
    acc.secrets_blob = vault.encrypt_obj(secrets)
    acc = db.upsert_account(acc)
    console.print(f"[green]added[/green] {acc.email} (id={acc.id[:8]})")


@app.command()
def register(
    count: int = typer.Argument(1, min=1, max=50, help="how many accounts to register"),
    concurrency: int = typer.Option(2, "-c", "--concurrency", min=1, max=10),
    headed: bool = typer.Option(False, "--headed", help="show the browser"),
    no_persist: bool = typer.Option(False, "--no-persist", help="do not save to DB"),
    proxy: str = typer.Option(
        "",
        "--proxy",
        help="Override proxy URL for this run (e.g. http://127.0.0.1:7890, "
             "socks5://127.0.0.1:1080, or 'none' to disable). "
             "Defaults to TAM_PROXY env var or http://127.0.0.1:10808.",
    ),
) -> None:
    """Register one or more new Trae accounts."""
    import os
    if proxy:
        os.environ["TAM_PROXY"] = proxy
        console.print(f"[dim]using proxy: {proxy}[/dim]")
    from .register import register_batch
    results = asyncio.run(
        register_batch(
            count, concurrency, headless=not headed, persist=not no_persist,
        )
    )
    ok = sum(1 for r in results if r.success)
    console.print(f"[green]{ok}/{len(results)}[/green] registered")
    # If everything failed with connection errors, print a helpful hint.
    if ok == 0 and results:
        errs = [r.error or "" for r in results]
        if any("connect" in e.lower() or "proxy" in e.lower() for e in errs):
            from .config import get_proxy
            console.print(
                f"[yellow]hint:[/yellow] all attempts failed with connection errors.\n"
                f"  current proxy: [bold]{get_proxy() or '(none)'}[/bold]\n"
                f"  verify the proxy port is correct (Clash=7890, v2rayN=10809, etc.)\n"
                f"  override with: tam register 1 --proxy http://127.0.0.1:7890\n"
                f"  or disable:     tam register 1 --proxy none"
            )
    for r in results:
        if r.success:
            console.print(f"  [green]✓[/green] {r.email}")
        else:
            console.print(f"  [red]✗[/red] {r.email or '?'}: {r.error}")


@app.command()
def usage(
    account_id: Optional[str] = typer.Argument(None, help="account id (defaults to current)"),
) -> None:
    """Query Trae usage for an account (or the current one)."""
    from .trae_api import TraeApiClient

    a = _find_account(account_id) if account_id else _current_account()
    if a is None:
        console.print("[red]no account to query[/red]")
        raise typer.Exit(1)
    async def _go():
        async with TraeApiClient.for_account(a) as client:
            summary = await client.get_usage_summary_by_token()
            delta = client.get_refreshed_secrets_delta()
            return summary, delta
    summary, delta = asyncio.run(_go())
    # If the JWT was refreshed during this call, persist the new credentials
    # back to the database so subsequent queries don't have to refresh again
    # (and `tam switch` has fresh tokens to write into storage.json).
    if delta:
        try:
            from .vault import decrypt_obj, encrypt_obj
            secrets = decrypt_obj(a.secrets_blob)
            # Preserve user_id / email / username / host from the existing
            # login_info — get_refreshed_secrets_delta leaves them blank
            # because the TraeApiClient doesn't know the account metadata.
            old_login_info = secrets.get("login_info") or {}
            new_login_info = delta.get("login_info") or {}
            for k in ("user_id", "email", "username", "avatar_url", "host"):
                if not new_login_info.get(k):
                    new_login_info[k] = old_login_info.get(k, "")
            secrets.update(delta)
            secrets["login_info"] = new_login_info
            a.secrets_blob = encrypt_obj(secrets)
            db.upsert_account(a)
            console.print(f"[dim]jwt refreshed and persisted (expires={delta.get('token_expired_at', '')})[/dim]")
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]jwt refreshed but persist failed: {e}[/yellow]")
    t = Table("metric", "limit", "used", "left")
    t.add_row("plan", summary.plan_type, "", "")
    t.add_row("fast requests", str(summary.fast_request_limit),
              f"{summary.fast_request_used:.1f}", f"{summary.fast_request_left:.1f}")
    t.add_row("slow requests", str(summary.slow_request_limit),
              f"{summary.slow_request_used:.1f}", f"{summary.slow_request_left:.1f}")
    t.add_row("advanced models", str(summary.advanced_model_limit),
              f"{summary.advanced_model_used:.1f}", f"{summary.advanced_model_left:.1f}")
    t.add_row("autocomplete", str(summary.autocomplete_limit),
              f"{summary.autocomplete_used:.1f}", f"{summary.autocomplete_left:.1f}")
    if summary.extra_fast_request_limit:
        t.add_row("[bold]extra[/bold] " + (summary.extra_package_name or ""),
                  str(summary.extra_fast_request_limit),
                  f"{summary.extra_fast_request_used:.1f}",
                  f"{summary.extra_fast_request_left:.1f}")
    console.print(t)


# ---------------------------------------------------------------------------
@app.command()
def set_path(path: str = typer.Argument(..., help="path to Trae.exe / Trae.app")) -> None:
    """Set the Trae IDE executable path."""
    set_trae_exe_path(path)
    console.print(f"[green]saved[/green] Trae path: {path}")


@app.command(name="path")
def show_path() -> None:
    """Show the configured Trae executable path."""
    p = get_trae_exe_path()
    console.print(p or "[dim](not configured)[/dim]")


@app.command()
def info() -> None:
    """Show environment / configuration info."""
    from .config import get_license_dat_path, get_proxy, get_trae_config_dir
    from .profile import get_profile_root
    console.print(f"tam version      : {__version__}")
    console.print(f"Trae data dir   : {get_trae_data_dir()}")
    console.print(f"Trae config dir : {get_trae_config_dir()}")
    console.print(f"license.dat     : {get_license_dat_path()}")
    console.print(f"Trae exe        : {get_trae_exe_path() or '(not configured)'}")
    console.print(f"profile root    : {get_profile_root()}")
    console.print(f"proxy           : {get_proxy() or '(direct, no proxy)'}")
    console.print(f"db path         : {db.get_db_path() if hasattr(db, 'get_db_path') else '(internal)'}")


# ---------------------------------------------------------------------------
# Profile management (per-account state isolation)
# ---------------------------------------------------------------------------
profile_app = typer.Typer(help="Manage per-account Trae profiles (chat history / cookies / license.dat).")
app.add_typer(profile_app, name="profile")


@profile_app.command("list")
def profile_list() -> None:
    """List all stored account profiles."""
    from .profile import list_profiles
    profiles = list_profiles()
    if not profiles:
        console.print("[dim]no profiles yet — switch accounts to create them[/dim]")
        return
    t = Table("account_id", "email", "last_backup", "last_restore", "size")
    for p in profiles:
        t.add_row(
            p["account_id"][:12],
            p["email"],
            _ts(p["last_backup_at"]),
            _ts(p["last_restore_at"]),
            _human_size(p["size_bytes"]),
        )
    console.print(t)


@profile_app.command("show")
def profile_show(
    account_id: str = typer.Argument(..., help="account id (or unique prefix)"),
) -> None:
    """Show details of one account's profile."""
    from .profile import has_profile, read_meta, resolve_profile_paths
    a = _find_account(account_id)
    if a is None:
        console.print(f"[red]no account matching '{account_id}'[/red]")
        raise typer.Exit(1)
    if not has_profile(a.id):
        console.print(f"[yellow]no profile yet for {a.email}[/yellow]")
        return
    paths = resolve_profile_paths(a.id)
    meta = read_meta(a.id)
    console.print(f"[bold]account[/bold] {a.email}  (id={a.id[:8]})")
    console.print(f"profile dir   : {paths.root}")
    console.print(f"email        : {meta.email}")
    console.print(f"last backup  : {_ts(meta.last_backup_at)}")
    console.print(f"last restore : {_ts(meta.last_restore_at)}")
    console.print(f"size         : {_human_size(_dir_size_safe(paths.root))}")
    console.print("[dim]files:[/dim]")
    for rel, p in paths.state_files.items():
        mark = "[green]✓[/green]" if p.exists() else "[dim]·[/dim]"
        console.print(f"  {mark} {rel}")
    if paths.license_dat.exists():
        console.print("  [green]✓[/green] license.dat")
    console.print("[dim]dirs:[/dim]")
    for rel, p in paths.state_dirs.items():
        mark = "[green]✓[/green]" if p.exists() and any(p.iterdir()) else "[dim]·[/dim]"
        console.print(f"  {mark} {rel}")


@profile_app.command("backup")
def profile_backup(
    account_id: str = typer.Argument(..., help="account id (or unique prefix)"),
) -> None:
    """Backup the CURRENT Trae state into the given account's profile."""
    from .profile import backup_profile
    a = _find_account(account_id)
    if a is None:
        console.print(f"[red]no account matching '{account_id}'[/red]")
        raise typer.Exit(1)
    sw = Switcher()
    sw.ctl.kill()  # ensure Trae is not running
    res = backup_profile(pathlib.Path(get_trae_data_dir()), a.id, email=a.email)
    console.print(
        f"[green]backed up[/green] {len(res['copied_files'])} files + "
        f"{len(res['copied_dirs'])} dirs → profile/{a.id[:8]}"
    )


@profile_app.command("delete")
def profile_delete(
    account_id: str = typer.Argument(..., help="account id (or unique prefix)"),
) -> None:
    """Delete an account's profile (chat history etc.). Account itself is kept."""
    from .profile import delete_profile, has_profile
    a = _find_account(account_id)
    if a is None:
        console.print(f"[red]no account matching '{account_id}'[/red]")
        raise typer.Exit(1)
    if not has_profile(a.id):
        console.print(f"[yellow]no profile for {a.email}[/yellow]")
        return
    if delete_profile(a.id):
        console.print(f"[green]deleted[/green] profile for {a.email}")
    else:
        console.print(f"[red]delete failed[/red]")


# ---------------------------------------------------------------------------
def _ts(unix_ts: int) -> str:
    if not unix_ts:
        return "-"
    import datetime as _dt
    return _dt.datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d %H:%M:%S")


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _dir_size_safe(p) -> int:
    from pathlib import Path
    if not isinstance(p, Path):
        p = Path(p)
    if not p.exists():
        return 0
    total = 0
    for f in p.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port"),
) -> None:
    """Start the local web dashboard."""
    import uvicorn
    console.print(f"[green]starting dashboard[/green] at http://{host}:{port}")
    uvicorn.run(
        "trae_account_manager.web.app:app",
        host=host, port=port, log_level="info",
    )


# ---------------------------------------------------------------------------
def _find_account(prefix: str) -> Account | None:
    """Match an account by exact id, id prefix, or email (case-insensitive)."""
    if not prefix:
        return None
    a = db.get_account(prefix)
    if a:
        return a
    by_email = db.get_account_by_email(prefix)
    if by_email:
        return by_email
    for a in db.list_accounts():
        if a.id.startswith(prefix) or a.email.lower() == prefix.lower():
            return a
    return None


def _current_account() -> Account | None:
    cid = db.get_current_account_id()
    return db.get_account(cid) if cid else None


def main() -> None:
    app()


if __name__ == "__main__":
    main()
