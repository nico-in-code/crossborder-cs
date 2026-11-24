"""One-time remote PostgreSQL bootstrap and SSH key installation.

The root password is read from REMOTE_ROOT_PASSWORD and is never written to disk.
Only the dedicated crossborder_cs_* roles and databases are touched.
"""

from __future__ import annotations

import os
import secrets
import sys
import urllib.parse
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

HOST = os.getenv("REMOTE_SSH_HOST", "36.139.119.132")
SSH_PORT = int(os.getenv("REMOTE_SSH_PORT", "22"))
SSH_USER = os.getenv("REMOTE_SSH_USER", "root")
LOCAL_PG_PORT = os.getenv("REMOTE_PG_PORT", "55432")
REMOTE_PG_PORT = os.getenv("REMOTE_PG_REMOTE_PORT", "5433")
KEY_PATH = Path(
    os.getenv("REMOTE_SSH_KEY", str(Path.home() / ".ssh" / "crossborder_cs_ed25519"))
)
APP_ROLE = "crossborder_cs_app"
RO_ROLE = "crossborder_cs_ro"
DEV_DB = "crossborder_cs"
TEST_DB = "crossborder_cs_test"


def run(client: paramiko.SSHClient, command: str, *, timeout: int = 60) -> tuple[str, str]:
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace").strip()
    err = stderr.read().decode("utf-8", "replace").strip()
    if out:
        print(f"  {out}")
    if err:
        print(f"  STDERR: {err}")
    return out, err


def psql_stdin(client: paramiko.SSHClient, sql: str, database: str = "postgres") -> None:
    _stdin, stdout, stderr = client.exec_command(
        f"sudo -u postgres psql -v ON_ERROR_STOP=1 -d {database} -q", timeout=60
    )
    _stdin.write(sql)
    _stdin.channel.shutdown_write()
    err = stderr.read().decode("utf-8", "replace").strip()
    out = stdout.read().decode("utf-8", "replace").strip()
    if err:
        raise RuntimeError(f"remote SQL failed: {err}")
    if out:
        print(f"  {out}")


def database_exists(client: paramiko.SSHClient, name: str) -> bool:
    out, _ = run(
        client,
        f"sudo -u postgres psql -Atc \"SELECT 1 FROM pg_database WHERE datname='{name}'\"",
    )
    return out.strip() == "1"


def database_owner(client: paramiko.SSHClient, name: str) -> str:
    out, _ = run(
        client,
        f"sudo -u postgres psql -Atc \"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='{name}'\"",
    )
    return out.strip()


def install_key(client: paramiko.SSHClient) -> None:
    pub_path = Path(str(KEY_PATH) + ".pub")
    if not pub_path.exists():
        raise RuntimeError(f"missing public key: {pub_path}")
    pub = pub_path.read_text(encoding="utf-8").strip()
    current, _ = run(client, "cat ~/.ssh/authorized_keys 2>/dev/null || true")
    if pub in current:
        print("  SSH key already authorized")
        return
    sftp = client.open_sftp()
    tmp = "/root/.ssh/authorized_keys.crossborder.tmp"
    with sftp.file(tmp, "w") as fh:
        fh.write((current + "\n" if current else "") + pub + "\n")
    sftp.chmod(tmp, 0o600)
    sftp.close()
    run(client, f"mv {tmp} /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys")
    print("  SSH key installed for passwordless tunnel")


def test_key_login() -> None:
    if not KEY_PATH.exists():
        raise RuntimeError(f"missing private key: {KEY_PATH}")
    probe = paramiko.SSHClient()
    probe.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    probe.connect(
        HOST,
        port=SSH_PORT,
        username=SSH_USER,
        key_filename=str(KEY_PATH),
        allow_agent=False,
        look_for_keys=False,
        timeout=15,
    )
    probe.exec_command("true")
    probe.close()
    print("  SSH key login verified")


def write_env(app_password: str, ro_password: str) -> None:
    template = ENV_EXAMPLE.read_text(encoding="utf-8")
    quote = urllib.parse.quote
    dev_url = (
        f"postgresql+psycopg://{APP_ROLE}:{quote(app_password, safe='')}"
        f"@127.0.0.1:{LOCAL_PG_PORT}/{DEV_DB}"
    )
    test_url = (
        f"postgresql+psycopg://{APP_ROLE}:{quote(app_password, safe='')}"
        f"@127.0.0.1:{LOCAL_PG_PORT}/{TEST_DB}"
    )
    ro_url = (
        f"postgresql+psycopg://{RO_ROLE}:{quote(ro_password, safe='')}"
        f"@127.0.0.1:{LOCAL_PG_PORT}/{DEV_DB}"
    )
    replacements = {
        "DATABASE_URL=postgresql+psycopg://crossborder_cs_app:CHANGE_ME@127.0.0.1:55432/crossborder_cs": f"DATABASE_URL={dev_url}",
        "TEST_DATABASE_URL=postgresql+psycopg://crossborder_cs_app:CHANGE_ME@127.0.0.1:55432/crossborder_cs_test": f"TEST_DATABASE_URL={test_url}",
        "MCP_DATABASE_URL=postgresql+psycopg://crossborder_cs_ro:CHANGE_ME@127.0.0.1:55432/crossborder_cs": f"MCP_DATABASE_URL={ro_url}",
        "SESSION_SECRET=replace-with-random-value": f"SESSION_SECRET={secrets.token_urlsafe(32)}",
        "MCP_CONTEXT_SECRET=replace-with-another-random-value": f"MCP_CONTEXT_SECRET={secrets.token_urlsafe(32)}",
        "REMOTE_PG_PORT=55432": f"REMOTE_PG_PORT={LOCAL_PG_PORT}\nREMOTE_PG_REMOTE_PORT={REMOTE_PG_PORT}",
    }
    lines = []
    for line in template.splitlines():
        lines.append(replacements.get(line, line))
    existing = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    if existing and "DATABASE_URL=postgresql+psycopg://crossborder_cs_app:" in existing and "CHANGE_ME" not in existing:
        print("  .env already contains database URLs; refusing to overwrite")
        return
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass
    print(f"  wrote {ENV_FILE} (secret values not printed)")


def main() -> int:
    password = os.getenv("REMOTE_ROOT_PASSWORD")
    if not password:
        print("REMOTE_ROOT_PASSWORD is required", file=sys.stderr)
        return 2
    app_password = secrets.token_urlsafe(24)
    ro_password = secrets.token_urlsafe(24)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Connecting to {SSH_USER}@{HOST}:{SSH_PORT} ...")
    client.connect(
        HOST,
        port=SSH_PORT,
        username=SSH_USER,
        password=password,
        timeout=20,
        auth_timeout=20,
        banner_timeout=20,
    )
    try:
        print("Checking dedicated resources ...")
        roles, _ = run(
            client,
            "sudo -u postgres psql -Atc \"SELECT rolname FROM pg_roles WHERE rolname IN "
            "('crossborder_cs_app','crossborder_cs_ro') ORDER BY 1\"",
        )
        existing_roles = {r for r in roles.splitlines() if r.strip()}
        if existing_roles:
            print(f"  existing dedicated roles: {sorted(existing_roles)}")

        print("Creating/updating dedicated roles ...")
        sql = f"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
    CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{app_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  ELSE
    ALTER ROLE {APP_ROLE} WITH LOGIN PASSWORD '{app_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RO_ROLE}') THEN
    CREATE ROLE {RO_ROLE} LOGIN PASSWORD '{ro_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  ELSE
    ALTER ROLE {RO_ROLE} WITH LOGIN PASSWORD '{ro_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END
$$;
"""
        psql_stdin(client, sql)

        for db in (DEV_DB, TEST_DB):
            if database_exists(client, db):
                owner = database_owner(client, db)
                if owner != APP_ROLE:
                    raise RuntimeError(
                        f"database {db} exists but is owned by {owner}; refusing to touch it"
                    )
                print(f"  database {db} already exists (owner {owner})")
            else:
                run(
                    client,
                    f"sudo -u postgres createdb -O {APP_ROLE} -E UTF8 {db}",
                )
                print(f"  created database {db}")
            psql_stdin(
                client,
                f"GRANT CONNECT ON DATABASE {db} TO {RO_ROLE};\n"
                f"ALTER DATABASE {db} SET timezone TO 'UTC';\n",
            )
            psql_stdin(
                client,
                f"GRANT USAGE ON SCHEMA public TO {RO_ROLE};\n",
                database=db,
            )

        install_key(client)
    finally:
        client.close()

    test_key_login()
    write_env(app_password, ro_password)
    print("Bootstrap complete. Start the tunnel before running migrations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())